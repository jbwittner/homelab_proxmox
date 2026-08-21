"""L'ordre du déploiement, et ce qui se passe entre les sections.

L'ordre est une DONNÉE, relisible d'un coup — et donc vérifiable : un test
constate que chaque prérequis est déclaré plus tôt, ce qu'aucune relecture ne
garantit.

DEUX CHOSES SE JOUENT ICI, ET AUCUNE N'EST DANS LES ÉTAPES.

**L'ordre.** Il n'est pas cosmétique.

  - Le montage est constaté avant toute pose, sinon on copie du néant.
  - L'outillage du nœud passe avant la section V, parce que c'est le nœud qui
    télécharge et qui vérifie : sans `gnupg`, il n'y a rien à vérifier.
  - Les secrets passent avant le premier démarrage de Forgejo. C'est l'ordre
    le plus important du fichier : démarrer d'abord, ce serait laisser Forgejo
    en générer lui-même et tenter de réécrire un `app.ini` en lecture seule.
  - La base passe avant le service, sinon le premier démarrage n'est qu'une
    suite d'échecs de connexion.
  - La première sauvegarde précède la copie hors-site, sinon la copie initiale
    n'a rien à transférer.
  - Les contrôles ferment le parcours, sinon ils répondent sur l'état d'avant.

**Les effets.** Les étapes DÉCLARENT qu'un redémarrage ou un rechargement sera
nécessaire ; c'est ici qu'on dit ce que cela veut dire concrètement, et les
barrières disent QUAND le faire. Quatre barrières, quatre raisons : redémarrer
le CT avant que la section B ne regarde son montage, recharger systemd avant
d'armer quoi que ce soit dans le CT, redémarrer Forgejo après que ses secrets
et son binaire sont posés, puis recharger systemd sur le nœud avant d'y armer
le timer. Armer une unité que systemd n'a pas relue arme la version
précédente — et cela ne se voit qu'à 2h45.

**`restart` l'emporte sur `reload`.** Le rechargement de PostgreSQL est demandé
à chaque déploiement, sans condition : ses fichiers de configuration sont des
symlinks vers le dépôt, un `git pull` a donc pu en changer le contenu sans
qu'aucun `check()` puisse s'en apercevoir. Mais si un redémarrage a déjà eu
lieu, le rechargement n'a plus rien à apporter.
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
from fjtool.steps import horssite as F
from fjtool.steps import hote as D
from fjtool.steps import postgres as P
from fjtool.steps import prerequis as A
from fjtool.steps import retraits as H
from fjtool.steps import secrets as G

# Le rechargement de PostgreSQL, demandé à chaque parcours.
EFFETS_FINAUX = (B.EFFET_PG_REFRESH,)

# Chemins d'installation sur le nœud, absolus : le PATH de systemd est minimal.
UNITE_HORSSITE = Path("/etc/systemd/system/fjbk-offsite.service")
TIMER_HORSSITE = Path("/etc/systemd/system/fjbk-offsite.timer")
DROPIN_HORSSITE = Path("/etc/systemd/system/fjbk-offsite.service.d/10-noeud.conf")

# Ce que le script communautaire aurait posé si quelqu'un l'avait joué. Retiré
# sous condition que notre propre unité soit conforme — voir section H.
UNITE_COMMUNAUTAIRE = Path("/etc/systemd/system/gitea.service")


def etapes(ctx: Context) -> list:
    """La liste ordonnée. Une donnée, pas une suite d'appels.

    Les valeurs qui viennent de l'unité du dépôt sont lues ICI, une fois : le
    dépôt fait foi, c'est ce que le déploiement s'apprête à poser.
    """
    unite = ctx.paths.host_src / "fjbk-offsite.service"
    rclone_bin = F.unit_env(unite, "FJBK_OFFSITE_RCLONE", "/usr/bin/rclone")
    rclone_conf = F.unit_env(
        unite, "FJBK_OFFSITE_CONFIG", "/root/.config/rclone/rclone.conf")
    cle = F.unit_env(
        unite, "FJBK_OFFSITE_KEY", "/root/.config/rclone/pgsql-backups.json")
    remote = F.unit_env(unite, "FJBK_OFFSITE_REMOTE", "gcs")

    return [
        # ── A. les prérequis du conteneur ────────────────────────────────
        A.ConteneurDemarre(),
        A.Protection(),
        A.Nesting(),
        A.Onboot(),
        A.Mp1Depot(),
        A.Mp2Sauvegardes(),
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
        D.PaquetHote("rclone", Path(rclone_bin),
                     "sans lui, aucune copie hors-site"),
        D.FjHote(),
        D.FjtoolHote(),

        # ── B. la pose dans le conteneur ─────────────────────────────────
        B.MontageVisible(),
        B.MontageLectureSeule(),
        B.PaquetCT("sudo", "/usr/bin/sudo"),
        B.PaquetCT("python3-minimal", "/usr/bin/python3"),
        B.PaquetCT("git", "/usr/bin/git"),
        B.PaquetCT("git-lfs", "/usr/bin/git-lfs"),
        B.PaquetCT("postgresql", "/usr/bin/psql"),
        B.UtilisateurGit(),
        # L'arborescence, avec ses modes. `secrets` en 0700 root:git : le
        # répertoire qui porte la clé de chiffrement de la base ne se lit pas
        # par accident.
        B.Repertoire(CT_OPT, "root:root", "755"),
        B.Repertoire(CT_ETC, "root:git", "750"),
        B.Repertoire(CT_SECRETS, "root:git", "700"),
        B.Repertoire(CT_DATA, "git:git", "750"),
        B.ClusterDetecte(),
        B.SymlinkConf("10-forgejo.conf", "conf.d"),
        B.SymlinkConf("pg_hba.conf"),
        B.SymlinkConf("pg_ident.conf"),
        # app.ini est une COPIE, en 0640 root:git — voir l'en-tête de
        # steps/conteneur.py pour la raison.
        B.FichierCT(
            "app.ini", "/etc/forgejo/app.ini", 0o640,
            proprietaire="root:git",
            effets=frozenset({B.EFFET_FORGEJO_RESTART}),
            requires=(B.SENTINELLE, CT_ETC),
        ),
        B.FichierCT("forgejo.service",
                    "/etc/systemd/system/forgejo.service", 0o644),
        B.FichierCT("fj-backup.service",
                    "/etc/systemd/system/fj-backup.service", 0o644),
        B.FichierCT("fj-backup.timer",
                    "/etc/systemd/system/fj-backup.timer", 0o644),
        B.MoteurCT(),
        B.LanceurCT(),
        Barrier("unités du CT rechargées", "B"),

        # ── V. l'installation binaire épinglée ───────────────────────────
        V.VersionEpinglee(),
        V.CleDePublication(),
        V.BinaireForgejo(),
        V.SymlinkForgejo(),
        V.DurcissementGit(),

        # ── P. la base, dans le cluster co-localisé ──────────────────────
        P.BaseEtRole(),
        P.AclConnect(),
        P.ConnexionForgejo(),

        # ── G. les secrets, AVANT le premier démarrage ───────────────────
        # L'ordre le plus important du fichier : démarrer d'abord laisserait
        # Forgejo générer les siens et tenter de réécrire un app.ini en
        # lecture seule.
        G.SecretsForgejo(),
        Barrier("Forgejo prêt à démarrer", "G"),

        # ── B. le service, une fois que tout ce dont il dépend est posé ──
        B.ServiceForgejoArme(),
        B.TimerSauvegardeArme(),

        # ── G. la première sauvegarde, AVANT le hors-site ────────────────
        G.PremiereSauvegarde(),

        # ── E/F. la copie hors-site ──────────────────────────────────────
        F.CleGCP(Path(cle)),
        F.ConfigRclone(Path(rclone_conf), remote=remote, cle=Path(cle)),
        F.SourceHorsSite(),
        F.DropInNoeud(DROPIN_HORSSITE, node=_noeud()),
        F.UniteHorsSite("fjbk-offsite.service", UNITE_HORSSITE),
        F.UniteHorsSite("fjbk-offsite.timer", TIMER_HORSSITE),
        Barrier("unités du nœud rechargées", "F"),
        F.ArmementHorsSite(),

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
        C.AucuneSocketTcp(),
        C.AclApresMigration(),
        C.InscriptionFermee(),
        C.ProxyDeConfiance(),
        C.VersionEnService(),
        C.JournalForgejo(),
        C.TimerSauvegarde(),
        C.TimerHorsSite(),
    ]


def _noeud() -> str:
    import os

    return os.uname().nodename.split(".")[0]


# ─── ce que les effets déclarés veulent dire ─────────────────────────────────


def brancher_effets(ctx: Context) -> None:
    """Traduit les effets déclarés en gestes concrets.

    Les étapes disent « il faudra redémarrer » sans savoir comment ; c'est ici
    qu'on le sait. Cette séparation est ce qui permet à quatre copies d'unité
    de ne provoquer qu'un seul rechargement.
    """
    ctx.on_effect(A.EFFET_REBOOT, _redemarrer_ct)
    ctx.on_effect(B.EFFET_DAEMON_RELOAD, _daemon_reload_ct)
    ctx.on_effect(B.EFFET_PG_RESTART, _restart_postgresql)
    ctx.on_effect(B.EFFET_PG_REFRESH, _reload_postgresql)
    ctx.on_effect(B.EFFET_FORGEJO_RESTART, _restart_forgejo)
    ctx.on_effect(F.EFFET_RELOAD, _daemon_reload_hote)


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


def _restart_postgresql(ctx: Context) -> None:
    """`listen_addresses` ne se relit pas à chaud : un reload ne suffirait pas
    à la première pose, quand le drop-in et `pg_ident` viennent d'apparaître."""
    Systemd(ctx.runner.for_container(ctx.opts.ctid)).restart("postgresql")
    ctx.facts["postgresql_restarted"] = True


def _reload_postgresql(ctx: Context) -> None:
    if ctx.facts.get("postgresql_restarted"):
        return
    if ctx.opts.force_restart:
        _restart_postgresql(ctx)
        return
    Systemd(ctx.runner.for_container(ctx.opts.ctid)).reload("postgresql")


def _restart_forgejo(ctx: Context) -> None:
    """Forgejo ne recharge rien à chaud : `app.ini` et les secrets ne sont lus
    qu'au démarrage.

    Ne rien faire tant que l'unité n'est pas installée : `systemctl restart`
    sur une unité inconnue échoue, et cet échec-là surviendrait au premier
    déploiement, c'est-à-dire exactement quand il n'y a rien à redémarrer.
    """
    systemd = Systemd(ctx.runner.for_container(ctx.opts.ctid))
    if not systemd.exists("forgejo.service"):
        info("  forgejo.service pas encore installé — rien à redémarrer")
        return
    systemd.restart("forgejo")


def _daemon_reload_hote(ctx: Context) -> None:
    Systemd(ctx.runner).daemon_reload()


# ─── le déploiement ──────────────────────────────────────────────────────────


def deployer(ctx: Context) -> list[Report]:
    """Un parcours, trois modes. Renvoie le bilan."""
    brancher_effets(ctx)
    return traverse(etapes(ctx), ctx, effets_finaux=EFFETS_FINAUX)
