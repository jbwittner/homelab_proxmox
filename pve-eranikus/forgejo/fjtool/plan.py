"""L'ordre du déploiement, et ce qui se passe entre les sections.

L'ordre est une DONNÉE, relisible d'un coup — et donc vérifiable : un test
constate que chaque prérequis est déclaré plus tôt, ce qu'aucune relecture ne
garantit.

CE QUE CE PLAN NE FAIT PAS, ET C'EST LA MOITIÉ DE SA VALEUR. Il ne crée pas de
base, il ne sauvegarde rien, il ne copie rien hors-site. La base de Forgejo est
un locataire du cluster mutualisé du CT 200 : sa création, sa sauvegarde et sa
copie hors-site sont l'affaire de `pve-eranikus/pgsql/pg`. Les dépôts, eux,
partent par `vzdump` du CT 400. Ce déploiement-ci pose Forgejo, et rien d'autre.

DEUX CHOSES SE JOUENT ICI, ET AUCUNE N'EST DANS LES ÉTAPES.

**L'ordre.** Il n'est pas cosmétique.

  - Le montage est constaté avant toute pose, sinon on copie du néant.
  - L'outillage du nœud passe avant la section V, parce que c'est le nœud qui
    télécharge et qui vérifie : sans `gnupg`, il n'y a rien à vérifier.
  - Le mot de passe de la base précède `app.ini`, qui le contient — le rendre
    sans lui produirait une configuration avec un marqueur en guise de secret.
  - Les secrets passent avant le premier démarrage. C'est l'ordre le plus
    important du plan : démarrer d'abord laisserait Forgejo en générer
    lui-même et tenter de réécrire un `app.ini` qu'il ne peut pas écrire.
  - La connexion à la base est éprouvée avant d'armer le service, sinon le
    premier démarrage n'est qu'une suite d'échecs d'authentification.
  - Les contrôles ferment le parcours, sinon ils répondent sur l'état d'avant.

**Les effets.** Les étapes DÉCLARENT qu'un redémarrage ou un rechargement sera
nécessaire ; c'est ici qu'on dit ce que cela veut dire concrètement, et les
barrières disent QUAND le faire. Trois barrières, trois raisons : redémarrer le
CT avant que la section B ne regarde son montage, recharger systemd avant
d'armer quoi que ce soit, redémarrer Forgejo après que sa configuration et ses
secrets sont posés. Armer une unité que systemd n'a pas relue arme la version
précédente.
"""

from __future__ import annotations

from pathlib import Path

from core.commands import Systemd
from core.converge import Barrier, Context, Report, traverse
from core.log import info
from fjtool.deploy import CT_DATA, CT_ETC, CT_OPT, CT_SECRETS
from fjtool.steps import binaire as V
from fjtool.steps import conteneur as B
from fjtool.steps import controles as C
from fjtool.steps import hote as D
from fjtool.steps import postgres as P
from fjtool.steps import prerequis as A
from fjtool.steps import retraits as H
from fjtool.steps import secrets as G

# Aucun effet final : il n'y a plus de configuration liée au montage dont le
# contenu puisse changer sous un `git pull` sans qu'un `check()` le voie.
# `app.ini` est RENDU, donc son empreinte est comparée à chaque passage.
EFFETS_FINAUX: tuple[str, ...] = ()

# Ce que le script communautaire aurait posé si quelqu'un l'avait joué.
UNITE_COMMUNAUTAIRE = Path("/etc/systemd/system/gitea.service")


def etapes(ctx: Context) -> list:
    """La liste ordonnée. Une donnée, pas une suite d'appels."""
    return [
        # ── A. les prérequis du conteneur ────────────────────────────────
        A.ConteneurDemarre(),
        A.Protection(),
        A.Nesting(),
        A.Onboot(),
        A.Mp1Depot(),
        A.Startup(),
        # Le seul endroit où le CT redémarre. Après tous les `pct set`, et
        # avant que quiconque ne regarde le montage.
        Barrier("prérequis du CT appliqués", "A"),

        # ── D/E. l'outillage du nœud, AVANT la section V ─────────────────
        # C'est le nœud qui télécharge et qui vérifie : sans `gnupg` ni
        # `python3`, la section V n'a pas de quoi travailler.
        D.ConfCtid(),
        D.Python3Hote(),
        D.PaquetHote("gnupg", Path("/usr/bin/gpg"),
                     "sans lui, aucune signature ne peut être vérifiée"),
        D.FjHote(),
        D.FjtoolHote(),

        # ── B. la pose dans le conteneur ─────────────────────────────────
        B.MontageVisible(),
        B.MontageLectureSeule(),
        B.PaquetCT("sudo", "/usr/bin/sudo"),
        B.PaquetCT("git", "/usr/bin/git"),
        B.PaquetCT("git-lfs", "/usr/bin/git-lfs"),
        # Le CLIENT seul : la base est ailleurs. Installer le serveur poserait
        # un cluster que personne n'utiliserait, et que quelqu'un finirait par
        # croire être celui de Forgejo.
        B.PaquetCT("postgresql-client", "/usr/bin/psql"),
        B.UtilisateurGit(),
        # L'arborescence, avec ses modes. `secrets` en 0700 root:git : le
        # répertoire qui porte la clé de chiffrement de l'instance et le mot
        # de passe de la base ne se lit pas par accident.
        B.Repertoire(CT_OPT, "root:root", "755"),
        B.Repertoire(CT_ETC, "root:git", "750"),
        B.Repertoire(CT_SECRETS, "root:git", "700"),
        B.Repertoire(CT_DATA, "git:git", "750"),
        B.FichierCT("forgejo.service",
                    "/etc/systemd/system/forgejo.service", 0o644),
        Barrier("unités du CT rechargées", "B"),

        # ── V. l'installation binaire épinglée ───────────────────────────
        V.VersionEpinglee(),
        V.CleDePublication(),
        V.BinaireForgejo(),
        V.SymlinkForgejo(),
        V.DurcissementGit(),

        # ── P. la base, LOCATAIRE du cluster mutualisé du CT 200 ─────────
        P.MotDePasseBase(),
        P.ConnexionBase(),

        # ── B. la configuration, qui porte le mot de passe ───────────────
        B.AppIni(),

        # ── G. les secrets, AVANT le premier démarrage ───────────────────
        G.SecretsForgejo(),
        Barrier("Forgejo prêt à démarrer", "G"),

        # ── B. le service, une fois que tout ce dont il dépend est posé ──
        B.ServiceForgejoArme(),

        # ── H. ce qui ne doit pas être là ────────────────────────────────
        H.RetraitOrphelin(
            UNITE_COMMUNAUTAIRE,
            remplace_par="« forgejo.service », posé depuis le dépôt",
            requires=("forgejo.service",),
        ),
        H.AucunAutoUpdate(),

        # ── G. les opérations à secret, sur demande explicite ────────────
        G.CompteAdmin(),

        # ── C. les contrôles, en dernier ─────────────────────────────────
        C.InscriptionFermee(),
        C.ProxyDeConfiance(),
        C.VersionEnService(),
        C.JournalForgejo(),
    ]


# ─── ce que les effets déclarés veulent dire ─────────────────────────────────


def brancher_effets(ctx: Context) -> None:
    """Traduit les effets déclarés en gestes concrets.

    Les étapes disent « il faudra redémarrer » sans savoir comment ; c'est ici
    qu'on le sait. Cette séparation est ce qui permet à plusieurs poses de ne
    provoquer qu'un seul rechargement.
    """
    ctx.on_effect(A.EFFET_REBOOT, _redemarrer_ct)
    ctx.on_effect(B.EFFET_DAEMON_RELOAD, _daemon_reload_ct)
    ctx.on_effect(B.EFFET_FORGEJO_RESTART, _restart_forgejo)


def _redemarrer_ct(ctx: Context) -> None:
    """Le redémarrage, et l'ATTENTE qui va avec.

    Rendre la main avant que le conteneur ne soit revenu ferait échouer la
    section suivante sur un `pct exec` dans un CT à l'arrêt — un échec qui ne
    ressemble en rien à sa cause.
    """
    from proxmox import Container

    conteneur = Container(ctx.runner, ctx.opts.ctid)
    info(f"  redémarrage du CT {ctx.opts.ctid} (points de montage relus)")
    conteneur.reboot()
    conteneur.wait_running()


def _daemon_reload_ct(ctx: Context) -> None:
    Systemd(ctx.runner.for_container(ctx.opts.ctid)).daemon_reload()


def _restart_forgejo(ctx: Context) -> None:
    """Forgejo ne recharge rien à chaud : `app.ini` et les secrets ne sont lus
    qu'au démarrage.

    Ne rien faire tant que l'unité n'est pas installée : `systemctl restart`
    sur une unité inconnue échoue, et cet échec surviendrait au premier
    déploiement, c'est-à-dire exactement quand il n'y a rien à redémarrer.
    """
    systemd = Systemd(ctx.runner.for_container(ctx.opts.ctid))
    if not systemd.exists("forgejo.service"):
        info("  forgejo.service pas encore installé — rien à redémarrer")
        return
    systemd.restart("forgejo")


# ─── le déploiement ──────────────────────────────────────────────────────────


def deployer(ctx: Context) -> list[Report]:
    """Un parcours, trois modes. Renvoie le bilan."""
    brancher_effets(ctx)
    return traverse(etapes(ctx), ctx, effets_finaux=EFFETS_FINAUX)
