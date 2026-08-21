"""Section P — la base de Forgejo, dans le cluster CO-LOCALISÉ du CT.

POURQUOI CO-LOCALISÉ, ET PAS SUR LE CLUSTER MUTUALISÉ DU CT 200. Mutualiser
créerait la chaîne `pve-eranikus → CT 200 → Forgejo → ArgoCD → cluster` : une
panne d'un nœud qui n'héberge même pas Forgejo bloquerait alors toute
réconciliation GitOps. L'autonomie de la source de vérité prime sur l'économie
de ressources — c'est la seule contrainte qui prime sur toutes les autres pour
ce service.

Deux étapes, et la seconde n'est pas un doublon de la première.

`BaseEtRole` crée ce qui manque, en jouant `init.sql` depuis le montage.

`AclConnect` vérifie que `REVOKE CONNECT … FROM PUBLIC` est TOUJOURS en
vigueur — et c'est le contrôle le plus utile de ce module, parce que les ACL
ne sont **ni dans un dump ni dans `globals.sql`** : une restauration les fait
disparaître en silence, la base remonte, tout a l'air normal, et l'isolation
n'est plus là. Le CT 200 a payé cette leçon ; on ne la repaie pas ici.
"""

from __future__ import annotations

from core.commands import Psql
from core.converge import Action, Outcome
from fjtool.deploy import MP
from fjtool.steps.conteneur import SENTINELLE

BASE = "forgejo"
ROLE = "forgejo"

NOM_BASE = "base forgejo"

# Droits d'une base dans un ACL PostgreSQL. LA CASSE EST SIGNIFIANTE :
#   C = CREATE      T = TEMPORARY      c = CONNECT
# « C » et « c » sont deux droits différents que seule la casse sépare. Passer
# la chaîne en minuscules les confond, et un PUBLIC qui peut CRÉER serait
# rapporté comme un PUBLIC qui peut SE CONNECTER. Défaut constaté sur le
# CT 200 le 21 août 2026.
DROIT_CONNECT = "c"


def public_peut_se_connecter(acl: str) -> bool:
    """Un `datacl` vide vaut « privilèges par défaut », donc PUBLIC connecté.

    Sinon on cherche une entrée dont le bénéficiaire est vide — la façon dont
    PostgreSQL écrit PUBLIC : `=Tc/postgres`. Chercher la sous-chaîne « =Tc/ »
    n'importe où matcherait aussi `forgejo=Tc/postgres`, un droit accordé au
    locataire lui-même, et conclurait à une perte d'isolation inexistante.

    Les entrées arrivent séparées par des ESPACES — c'est ce que produit
    `array_to_string(datacl, ' ')` — ou par des virgules si le tableau est
    rendu tel quel. Les deux sont acceptés : ne découper que sur la virgule ne
    verrait que l'entrée de tête, et manquerait une perte d'isolation dès que
    l'ordre change.
    """
    if not acl.strip():
        return True
    for entree in acl.strip().strip("{}").replace(",", " ").split():
        beneficiaire, _, droits = entree.partition("=")
        if beneficiaire == "" and DROIT_CONNECT in droits.split("/")[0]:
            return True
    return False


class EtapeP:
    section = "P"
    requires: tuple[str, ...] = (SENTINELLE, "cluster PostgreSQL")

    def skip_if(self, ctx) -> str | None:
        return None

    def _psql(self, ctx) -> Psql:
        return Psql(ctx.runner.for_container(ctx.opts.ctid))


def _jouer_init(ctx) -> None:
    """`init.sql` DEPUIS LE MONTAGE, jamais recopié en ordres isolés.

    Le fichier utilise `\\connect` et `\\gexec` : le découper en `-c` séparés
    changerait sa sémantique, et c'est justement la partie qui pose les REVOKE.
    """
    Psql(ctx.runner.for_container(ctx.opts.ctid)).run_file(f"{MP}/init.sql")


class BaseEtRole(EtapeP):
    """La base et son rôle. La BASE fait foi, pas le rôle.

    Un rôle sans base n'est pas une installation ; c'est la base qui porte les
    ACL, et c'est elle que Forgejo cherche au démarrage.
    """

    name = NOM_BASE

    def check(self, ctx) -> Outcome:
        psql = self._psql(ctx)
        if psql.database_exists(BASE):
            proprietaire = psql.database_owner(BASE)
            if proprietaire != ROLE:
                return Outcome(
                    "error",
                    f"la base {BASE} appartient à « {proprietaire} » et non à "
                    f"« {ROLE} » — Forgejo ne pourra pas migrer son schéma ; "
                    f"corriger à la main : ALTER DATABASE {BASE} OWNER TO {ROLE}",
                )
            return Outcome("ok", f"{BASE}, propriétaire {proprietaire}")
        return Outcome(
            "absent",
            f"la base {BASE} n'existe pas",
            (
                Action(
                    f"psql -f {MP}/init.sql",
                    _jouer_init,
                ),
            ),
        )


class AclConnect(EtapeP):
    """`REVOKE CONNECT … FROM PUBLIC`, toujours en vigueur.

    Se répare en rejouant `init.sql`, qui est idempotent : c'est le même
    fichier qui pose l'isolation et qui la rétablit, donc il n'existe pas deux
    définitions de ce qu'« isolé » veut dire.
    """

    name = "ACL de la base"
    requires = (SENTINELLE, "cluster PostgreSQL", NOM_BASE)

    def check(self, ctx) -> Outcome:
        acl = self._psql(ctx).database_acl(BASE)
        if not public_peut_se_connecter(acl):
            return Outcome("ok", acl)
        return Outcome(
            "drift",
            f"PUBLIC peut se connecter à {BASE} — isolation absente "
            f"(datacl : {acl or 'vide'}) ; les ACL ne sont pas dans un dump, "
            "une restauration vient peut-être de les effacer",
            (
                Action(
                    f"psql -f {MP}/init.sql (rétablit les REVOKE)",
                    _jouer_init,
                ),
            ),
        )


class ConnexionForgejo(EtapeP):
    """Forgejo peut-il RÉELLEMENT se connecter ?

    Toutes les étapes précédentes peuvent être vertes sans que ce soit vrai :
    il suffit que `pg_ident.conf` ne soit pas chargé, ou que la ligne `map=`
    manque de `pg_hba.conf`. L'échec ressemble alors à ceci, et ne nomme aucun
    des deux fichiers :

        FATAL:  Peer authentication failed for user "forgejo"

    On l'éprouve donc pour de bon, en se connectant SOUS L'UTILISATEUR `git`,
    exactement comme le service le fera. Une lecture (`SELECT 1`) : la
    connexion est ce qu'on teste, pas le droit d'écrire.
    """

    name = "connexion peer git → forgejo"
    requires = (SENTINELLE, "cluster PostgreSQL", NOM_BASE, "utilisateur git")

    def check(self, ctx) -> Outcome:
        ct = ctx.runner.for_container(ctx.opts.ctid)
        res = ct.read(
            "sudo", "-u", "git", "psql", "-d", BASE, "-U", ROLE,
            "-h", "/var/run/postgresql", "-tAc", "SELECT 1",
            check=False,
        )
        if res.ok and res.out == "1":
            return Outcome("ok", f"git → {ROLE}@{BASE} par socket Unix")
        return Outcome(
            "error",
            "l'utilisateur git ne peut pas se connecter — vérifier la ligne "
            f"« map=forgejo » de {MP}/pg_hba.conf et la correspondance de "
            f"{MP}/pg_ident.conf : " + (res.stderr.strip().splitlines() or [""])[0],
        )
