"""`fj status` — les maillons du montage, regardés ENSEMBLE.

`fj deploy --status` répond à « les fichiers sont-ils en place ». Celui-ci
répond à une autre question, et c'est celle qu'on se pose vraiment un matin :
**est-ce que ça marche ?**

TROIS MAILLONS, ET ILS PEUVENT SE ROMPRE CHACUN EN SILENCE :

    le service       Forgejo répond-il ?
    la version       est-ce bien celle qui est épinglée qui tourne ?
    la base          le locataire du CT 200 répond-il depuis ce conteneur ?

CE QUI N'EST PAS ICI, ET CE N'EST PAS UN OUBLI. Ni la sauvegarde, ni la copie
hors-site : la base de Forgejo est un locataire du cluster mutualisé, donc
c'est `pg status` qui en juge, sur le CT 200. Les dépôts, eux, partent par
`vzdump` du CT 400 et relèvent de la planification du nœud. Redoubler ces
contrôles ici donnerait deux verdicts sur un même objet, et le jour où ils
divergeraient personne ne saurait lequel croire.

**UN MAILLON NON CONSTATÉ EST UNE ALARME, pas un silence.** Un conteneur qui
n'a pas répondu ne vaut pas un conteneur sain.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.commands import Systemd
from core.runner import CommandError, ligne_utile


@dataclass
class Maillon:
    """Un constat. `ok=None` veut dire « pas pu regarder », jamais « ça va »."""

    nom: str
    ok: bool | None
    detail: str

    @property
    def verdict(self) -> str:
        if self.ok is None:
            return "?"
        return "OK" if self.ok else "KO"


@dataclass
class Etat:
    maillons: list[Maillon] = field(default_factory=list)

    def ajouter(self, nom: str, ok: bool | None, detail: str) -> None:
        self.maillons.append(Maillon(nom, ok, detail))


def relever(ctx) -> Etat:
    """Interroge les trois maillons. Ne modifie rien."""
    etat = Etat()
    ct = ctx.runner.for_container(ctx.opts.ctid)

    _service(etat, ct)
    _version(etat, ct, ctx)
    _base(etat, ct)
    return etat


def _service(etat: Etat, ct) -> None:
    try:
        actif = Systemd(ct).is_active("forgejo")
    except CommandError as exc:
        etat.ajouter("service Forgejo", None,
                     f"non constaté : {ligne_utile(exc.result.stderr)}")
        return
    etat.ajouter(
        "service Forgejo", actif,
        "actif" if actif else "inactif — la source de vérité ne répond pas",
    )


def _version(etat: Etat, ct, ctx) -> None:
    """La version SERVIE, comparée à l'épinglage du dépôt.

    Deux façons d'échouer, et elles ne se confondent pas : le binaire ne
    répond pas (installation cassée), ou il répond autre chose que ce que le
    dépôt épingle (quelqu'un a posé une version à la main).
    """
    from fjtool import version as V
    from fjtool.deploy import CT_BINAIRE

    res = ct.read(CT_BINAIRE, "--version", check=False)
    servie = V.version_installee(res.stdout) if res.ok else None
    if servie is None:
        etat.ajouter("version servie", None, f"{CT_BINAIRE} muet ou absent")
        return

    epinglee = V.lire(ctx.paths.version_file) if ctx.paths else None
    if epinglee is None:
        etat.ajouter("version servie", None, f"{servie} — aucun épinglage à comparer")
        return
    if servie == epinglee:
        etat.ajouter("version servie", True, f"{servie} (épinglée)")
        return
    etat.ajouter(
        "version servie", False,
        f"{servie} servie, {epinglee} épinglée — rejouer fj deploy",
    )


def _base(etat: Etat, ct) -> None:
    """Le locataire du CT 200, éprouvé DEPUIS ce conteneur.

    C'est le seul endroit d'où la question a un sens : la base peut très bien
    répondre au CT 200 lui-même et refuser celui-ci, faute d'une ligne dans
    `pg_hba.conf`.
    """
    from fjtool.steps.postgres import (
        BASE, ECHAPPE_PGPASS, HOTE_PG, MOT_DE_PASSE, PORT_PG, ROLE,
    )

    res = ct.read(
        "sh", "-c",
        # Même échappement que la sonde du déploiement : les deux-points d'un
        # mot de passe casseraient la ligne `.pgpass` et produiraient un
        # « password authentication failed » indiscernable d'un mauvais secret.
        'p=$(cat "$1" 2>/dev/null | ' + ECHAPPE_PGPASS + ') || exit 1; '
        'f=$(mktemp) || exit 1; '
        'chmod 600 "$f"; '
        'printf "%s:%s:%s:%s:%s\\n" "$2" "$3" "$4" "$5" "$p" > "$f"; '
        'PGPASSFILE="$f" psql "sslmode=require host=$2 port=$3 dbname=$4 '
        'user=$5" -tAc "SELECT 1"; '
        'rc=$?; rm -f "$f"; exit $rc',
        "sh", MOT_DE_PASSE, HOTE_PG, PORT_PG, BASE, ROLE,
        check=False,
    )
    if res.ok and res.out == "1":
        etat.ajouter("base (CT 200)", True, f"{ROLE}@{HOTE_PG}/{BASE}, SSL")
        return
    etat.ajouter("base (CT 200)", False, ligne_utile(res.stderr))


def alarmes(etat: Etat) -> list[Maillon]:
    """Tout ce qui n'est pas franchement bon. **`None` en fait partie.**"""
    return [m for m in etat.maillons if m.ok is not True]


def code_de_sortie(etat: Etat) -> int:
    return 1 if alarmes(etat) else 0


def render_etat(etat: Etat) -> str:
    """Un tableau. C'est une DONNÉE : il se recopie tel quel, sans horodatage.

    Les alarmes, elles, sont des messages SUR cette donnée : elles passent par
    la journalisation. La distinction vient de core.log, et la tenir permet de
    coller ce tableau dans un ticket sans traîner des horodatages.
    """
    largeur = max((len(m.nom) for m in etat.maillons), default=10)
    return "\n".join(
        f"  {m.verdict:<3} {m.nom:<{largeur}}  {m.detail}"
        for m in etat.maillons
    )
