"""`fj status` — les maillons du montage, regardés ENSEMBLE.

`fj deploy --status` répond à « les fichiers sont-ils en place ». Celui-ci
répond à une autre question, et c'est celle qu'on se pose vraiment un matin :
**est-ce que ça marche ?** Un déploiement peut être vert de partout pendant
qu'aucune sauvegarde ne part depuis trois semaines.

QUATRE MAILLONS, ET ILS PEUVENT SE ROMPRE CHACUN EN SILENCE :

    le service       Forgejo répond-il, et sur la version épinglée ?
    la sauvegarde    y en a-t-il une, et de quand ?
    le timer local   celui du CONTENEUR, qui la déclenche
    le hors-site     celui du NŒUD, qui l'emporte ailleurs

**UN MAILLON NON CONSTATÉ EST UNE ALARME, pas un silence.** Un bucket qui n'a
pas répondu ne vaut pas un bucket cohérent, et un timer qu'on n'a pas pu
interroger ne vaut pas un timer actif. C'est la règle qui manquait au CT 200
et qui lui a valu un hors-site armé sur un volume jamais vérifié.

LES SEUILS VIENNENT DES UNITÉS. La sauvegarde tourne à 02:45, donc au-delà de
26 heures une exécution a été manquée. Les écrire en dur ici les ferait
diverger de l'unité le jour où l'horaire change.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from core.commands import Systemd
from core.runner import CommandError

# 24 h de période, plus une marge pour le RandomizedDelaySec et un démarrage
# tardif du nœud. Au-delà, une exécution a été sautée.
SEUIL_SAUVEGARDE_H = 26
SEUIL_HORSSITE_H = 27


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


def relever(ctx, *, maintenant: float | None = None) -> Etat:
    """Interroge les quatre maillons. Ne modifie rien."""
    maintenant = maintenant if maintenant is not None else time.time()
    etat = Etat()
    ct = ctx.runner.for_container(ctx.opts.ctid)

    _service(etat, ct)
    _sauvegarde(etat, ct, ctx, maintenant)
    _timer_local(etat, ct)
    _timer_horssite(etat, ctx)
    return etat


def _service(etat: Etat, ct) -> None:
    from fjtool import version as V
    from fjtool.deploy import CT_BINAIRE

    try:
        actif = Systemd(ct).is_active("forgejo")
    except CommandError as exc:
        etat.ajouter("service Forgejo", None,
                     f"non constaté : {exc.result.stderr.strip()[:80]}")
        return
    if not actif:
        etat.ajouter("service Forgejo", False,
                     "inactif — la source de vérité ne répond pas")
        return
    res = ct.read(CT_BINAIRE, "--version", check=False)
    posee = V.version_installee(res.stdout) if res.ok else None
    etat.ajouter("service Forgejo", True, f"actif, {posee or 'version inconnue'}")


def _sauvegarde(etat: Etat, ct, ctx, maintenant: float) -> None:
    # `stat -c %Y` sur le lien RÉSOLU : `latest` est un symlink, et sa propre
    # date ne dit rien de celle de la sauvegarde qu'il désigne.
    res = ct.read(
        "sh", "-c", 'stat -Lc %Y "$1/latest" 2>/dev/null || true',
        "sh", ctx.opts.mp2_mount,
        check=False,
    )
    if not res.out:
        etat.ajouter(
            "sauvegarde locale", False,
            f"aucune sauvegarde dans {ctx.opts.mp2_mount}",
        )
        return
    try:
        age_h = int((maintenant - int(res.out)) // 3600)
    except ValueError:
        etat.ajouter("sauvegarde locale", None, f"date illisible : {res.out}")
        return
    if age_h > SEUIL_SAUVEGARDE_H:
        etat.ajouter(
            "sauvegarde locale", False,
            f"{age_h} h — au-delà de {SEUIL_SAUVEGARDE_H} h, "
            "une exécution a été manquée",
        )
        return
    etat.ajouter("sauvegarde locale", True, f"{age_h} h")


def _timer_local(etat: Etat, ct) -> None:
    try:
        arme = Systemd(ct).is_enabled("fj-backup.timer")
    except CommandError as exc:
        etat.ajouter("fj-backup.timer (CT)", None,
                     f"non constaté : {exc.result.stderr.strip()[:80]}")
        return
    etat.ajouter(
        "fj-backup.timer (CT)", arme,
        "actif" if arme else "inactif — plus aucune sauvegarde ne partira",
    )


def _timer_horssite(etat: Etat, ctx) -> None:
    if not ctx.opts.do_offsite:
        return
    try:
        arme = Systemd(ctx.runner).is_enabled("fjbk-offsite.timer")
    except CommandError as exc:
        etat.ajouter("fjbk-offsite.timer (nœud)", None,
                     f"non constaté : {exc.result.stderr.strip()[:80]}")
        return
    etat.ajouter(
        "fjbk-offsite.timer (nœud)", arme,
        "actif" if arme else "inactif — plus aucune copie hors-site ne partira",
    )


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
    lignes = []
    for maillon in etat.maillons:
        lignes.append(
            f"  {maillon.verdict:<3} {maillon.nom:<{largeur}}  {maillon.detail}"
        )
    return "\n".join(lignes)
