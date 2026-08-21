"""Section P — la base de Forgejo, LOCATAIRE DU CLUSTER MUTUALISÉ (CT 200).

CETTE SECTION NE CRÉE RIEN, et c'est le point. La base et le rôle sont créés
par l'outillage du CT 200 :

    pve-eranikus/pgsql/pg deploy --tenant forgejo

Ce déploiement-ci ne fait que **constater** que le locataire existe et qu'il
répond depuis le CT 400. Deux outils qui créeraient la même base de deux
façons finiraient par la créer de deux façons différentes — les ACL d'un côté,
pas de l'autre — et personne ne saurait laquelle fait foi.

POURQUOI MUTUALISÉ ET NON CO-LOCALISÉ. La première version co-localisait un
cluster dans le CT 400, au motif qu'une panne d'un nœud n'hébergeant pas
Forgejo ne devait pas bloquer la réconciliation GitOps. **Cet argument est
tombé quand Forgejo a rejoint `pve-eranikus`** : les deux conteneurs sont
désormais sur la même machine et tombent ensemble. Il ne restait que des
raisons secondaires, et elles ne valaient pas un second cluster PostgreSQL à
maintenir — d'autant qu'il aurait été en majeure 17 (Debian) face au 18 (PGDG)
du CT 200.

CE QUE LA MUTUALISATION COÛTE, ET IL FAUT LE DIRE : la connexion passe du
socket Unix en `peer` au TCP en `scram-sha-256`. Il y a donc désormais **un mot
de passe de base à faire vivre**, là où l'authentification par le noyau n'en
demandait aucun. Il est produit par `pg deploy --tenant`, rangé dans OpenBao,
et déposé dans `/etc/forgejo/secrets/db_password` — jamais dans le dépôt.
"""

from __future__ import annotations

from core.converge import Outcome
from core.runner import ligne_utile
from fjtool.deploy import CT_SECRETS
from fjtool.steps.conteneur import SENTINELLE

BASE = "forgejo"
ROLE = "forgejo"

# Le cluster mutualisé. L'IP est celle du CT 200, et elle est écrite ICI comme
# dans `ct/app.ini` : deux endroits, mais un contrôle les compare (voir
# `steps/controles.py`), plutôt qu'une constante partagée que le conteneur ne
# pourrait pas lire.
HOTE_PG = "192.168.1.56"
PORT_PG = "5432"

MOT_DE_PASSE = f"{CT_SECRETS}/db_password"

NOM_CONNEXION = "connexion à la base (CT 200)"

# Une ligne de `.pgpass` est `hôte:port:base:utilisateur:motdepasse` : les
# DEUX-POINTS séparent, l'ANTISLASH échappe. Un mot de passe qui en contient
# casse la ligne, psql lit une valeur tronquée, et le serveur répond :
#
#     FATAL:  password authentication failed for user "forgejo"
#
# c'est-à-dire EXACTEMENT le message d'un mauvais mot de passe. On part alors
# vérifier le secret — qui est juste — et rien n'y fait.
#
# L'ordre des deux substitutions n'est pas indifférent : l'antislash d'abord,
# sinon celui qu'on vient d'ajouter devant un deux-points serait échappé à son
# tour et rendrait la ligne fausse d'une autre façon.
ECHAPPE_PGPASS = r"sed -e 's/\\/\\\\/g' -e 's/:/\\:/g'"


class EtapeP:
    section = "P"
    requires: tuple[str, ...] = (SENTINELLE,)

    def skip_if(self, ctx) -> str | None:
        return None

    def _ct(self, ctx):
        return ctx.runner.for_container(ctx.opts.ctid)


class MotDePasseBase(EtapeP):
    """Le mot de passe du locataire, déposé — jamais généré ici.

    Le générer serait le générer une seconde fois : c'est `pg deploy --tenant`
    qui le produit, sur le CT 200, et lui seul sait ce que le rôle porte
    réellement. En fabriquer un ici donnerait deux vérités, dont une fausse.
    """

    name = "mot de passe de la base"
    requires = (SENTINELLE, CT_SECRETS)

    def check(self, ctx) -> Outcome:
        ct = self._ct(ctx)
        # Présence ET taille : un `touch` de dépannage laisse un fichier vide,
        # qui passerait un simple test d'existence et produirait un échec
        # d'authentification sans rapport apparent.
        vu = ct.read(
            "sh", "-c",
            'test -s "$1" && stat -c "%a %U:%G" "$1" || true',
            "sh", MOT_DE_PASSE,
            check=False,
        ).out
        if not vu:
            return Outcome(
                "error",
                f"{MOT_DE_PASSE} absent ou vide — c'est un secret : le créer "
                "sur le CT 200 avec « pg deploy --tenant forgejo », le ranger "
                "dans OpenBao, puis le déposer ici "
                "(doc/RUNBOOK.md section 3)",
            )
        if vu != "640 root:git":
            return Outcome(
                "drift",
                f"{MOT_DE_PASSE} est en {vu}, attendu 640 root:git",
                (
                    _corriger_mode(),
                ),
            )
        return Outcome("ok", f"{MOT_DE_PASSE} (640 root:git)")


def _corriger_mode():
    from core.converge import Action

    return Action(
        f"chown root:git && chmod 640 {MOT_DE_PASSE} (CT)",
        lambda c: _appliquer_mode(c),
    )


def _appliquer_mode(ctx) -> None:
    ct = ctx.runner.for_container(ctx.opts.ctid)
    ct.write("chown", "root:git", MOT_DE_PASSE)
    ct.write("chmod", "0640", MOT_DE_PASSE)


class ConnexionBase(EtapeP):
    """Forgejo peut-il RÉELLEMENT joindre sa base ?

    On l'éprouve pour de bon, depuis le conteneur, sous l'utilisateur `git`
    et avec le mot de passe déposé — exactement comme le service le fera.
    Tout le reste peut être vert sans que ce soit vrai : une ligne manquante
    dans le `pg_hba.conf` du CT 200, un locataire jamais créé, un pare-feu.

    L'échec est alors l'un de ceux-ci, et aucun ne nomme sa cause :

        FATAL:  no pg_hba.conf entry for host "192.168.1.57"
        FATAL:  password authentication failed for user "forgejo"
        FATAL:  database "forgejo" does not exist

    Le message rend la première ligne du refus telle quelle : c'est elle qui
    dit lequel des trois est en cause, et le reformuler la ferait perdre.
    """

    name = NOM_CONNEXION
    requires = (SENTINELLE, "utilisateur git", "mot de passe de la base",
                "postgresql-client (CT)")

    def check(self, ctx) -> Outcome:
        ct = self._ct(ctx)
        # Le mot de passe passe par PGPASSFILE, jamais par l'argv ni par
        # PGPASSWORD : un `ps` pendant l'opération le montrerait.
        res = ct.read(
            "sh", "-c",
            'p=$(cat "$1" | ' + ECHAPPE_PGPASS + ') || exit 1; '
            'f=$(mktemp) || exit 1; '
            'chmod 600 "$f"; '
            'printf "%s:%s:%s:%s:%s\\n" "$2" "$3" "$4" "$5" "$p" > "$f"; '
            'PGPASSFILE="$f" psql "sslmode=require host=$2 port=$3 '
            'dbname=$4 user=$5" -tAc "SELECT 1"; '
            'rc=$?; rm -f "$f"; exit $rc',
            "sh", MOT_DE_PASSE, HOTE_PG, PORT_PG, BASE, ROLE,
            check=False,
        )
        if res.ok and res.out == "1":
            return Outcome("ok", f"{ROLE}@{HOTE_PG}:{PORT_PG}/{BASE}, SSL")

        # `ligne_utile` et NON la première ligne : `pct` est un programme Perl
        # et ses avertissements de locale arrivent EN TÊTE, avant que psql
        # n'ait écrit un mot. Constaté le 21 août 2026 — le bilan annonçait
        # « perl: warning: Setting locale failed » comme cause d'un refus de
        # connexion, et envoyait corriger des locales parfaitement saines.
        cause = ligne_utile(res.stderr)
        return Outcome(
            "error",
            f"Forgejo ne joint pas sa base sur le CT 200 — {cause}\n"
            + _remede(cause),
        )


# À chaque cause son remède, et à un seul. Donner les trois à chaque fois
# obligerait à choisir soi-même celui qui s'applique — c'est-à-dire à refaire
# le diagnostic que le message vient de faire.
_REMEDES = (
    ("no pg_hba.conf entry",
     "la ligne du locataire manque, ou elle est APRÈS le reject.\n"
     "         L'ajouter dans pve-eranikus/pgsql/ct/pg_hba.conf, avant le\n"
     "         reject, puis : pg deploy"),
    ("password authentication failed",
     "le mot de passe déposé n'est pas celui du rôle.\n"
     "         Si « pg deploy --tenant forgejo » répond « existe — inchangé »,\n"
     "         c'est normal : il ne fait jamais tourner un secret déjà rangé.\n"
     "         Reprendre la valeur dans OpenBao, ou en poser une nouvelle par\n"
     "         ALTER ROLE depuis la porte peer du CT 200 — voir\n"
     "         doc/RUNBOOK.md section 3."),
    ("does not exist",
     "le locataire n'a jamais été créé : pg deploy --tenant forgejo (CT 200)"),
    ("could not connect",
     "le CT 200 ne répond pas sur 192.168.1.56:5432 — pct status 200"),
)


def _remede(cause: str) -> str:
    for motif, remede in _REMEDES:
        if motif in cause:
            return f"         {remede}"
    return (
        "         cause non reconnue — vérifier dans l'ordre : le CT 200 tourne,\n"
        "         le locataire existe (pg deploy --tenant forgejo), la ligne\n"
        "         hostssl est avant le reject, le mot de passe correspond."
    )
