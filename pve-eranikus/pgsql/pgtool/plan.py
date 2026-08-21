"""L'ordre du déploiement, et ce qui se passe entre les sections.

Le bash portait son ordre dans la suite des appels de `main()` : pour savoir
pourquoi la première sauvegarde précède le hors-site, il fallait lire neuf
cents lignes. Ici l'ordre est une DONNÉE, relisible d'un coup — et donc
vérifiable : un test constate que chaque prérequis est déclaré plus tôt, ce
qu'aucune relecture ne garantissait.

DEUX CHOSES SE JOUENT ICI, ET AUCUNE N'EST DANS LES ÉTAPES.

**L'ordre.** Il n'est pas cosmétique. La première sauvegarde précède la copie
hors-site, sinon la copie initiale n'a rien à transférer. Les contrôles ferment
le parcours, sinon ils répondent sur l'état d'avant. Le montage est constaté
avant toute pose, sinon on copie du néant.

**Les effets.** Les étapes DÉCLARENT qu'un redémarrage ou un rechargement sera
nécessaire ; c'est ici qu'on dit ce que cela veut dire concrètement, et les
barrières disent QUAND le faire. Trois barrières, trois raisons : redémarrer le
CT avant que la section B ne regarde son montage, recharger systemd avant
d'armer un timer dans le CT, puis avant d'en armer un sur le nœud. Armer une
unité que systemd n'a pas relue arme la version précédente — et cela ne se voit
qu'à 2h30.

**`restart` l'emporte sur `reload`.** Le rechargement de PostgreSQL est demandé
à chaque déploiement, sans condition : les fichiers de configuration sont des
symlinks vers le dépôt, un `git pull` a donc pu en changer le contenu sans
qu'aucun `check()` puisse s'en apercevoir. Mais si un redémarrage a déjà eu
lieu, le rechargement n'a plus rien à apporter.
"""

from __future__ import annotations

from pathlib import Path

from core.commands import Systemd
from core.converge import Barrier, Context, Report, Mode, traverse
from core.log import info, warn
from pgtool.deploy import MP
from pgtool.steps import conteneur as B
from pgtool.steps import controles as C
from pgtool.steps import horssite as F
from pgtool.steps import hote as D
from pgtool.steps import prerequis as A
from pgtool.steps import secrets as G

# Le rechargement de PostgreSQL, demandé à chaque parcours.
EFFETS_FINAUX = (B.EFFET_REFRESH,)

# Chemins d'installation sur le nœud, absolus : le PATH de systemd est minimal.
UNITE_HORSSITE = Path("/etc/systemd/system/pgbk-offsite.service")
TIMER_HORSSITE = Path("/etc/systemd/system/pgbk-offsite.timer")
SCRIPT_HORSSITE = Path("/usr/local/bin/pgbk-offsite")
DROPIN_HORSSITE = Path(
    "/etc/systemd/system/pgbk-offsite.service.d/10-noeud.conf"
)


def etapes(ctx: Context) -> list:
    """La liste ordonnée. Une donnée, pas une suite d'appels.

    Les valeurs qui viennent de l'unité du dépôt sont lues ICI, une fois : le
    dépôt fait foi, c'est ce que le déploiement s'apprête à poser.
    """
    unite = ctx.paths.host_src / "pgbk-offsite.service"
    rclone_bin = F.unit_env(unite, "PGBK_OFFSITE_RCLONE", "/usr/bin/rclone")
    rclone_conf = F.unit_env(
        unite, "PGBK_OFFSITE_CONFIG", "/root/.config/rclone/rclone.conf")
    cle = F.unit_env(
        unite, "PGBK_OFFSITE_KEY", "/root/.config/rclone/pgsql-backups.json")
    remote = F.unit_env(unite, "PGBK_OFFSITE_REMOTE", "gcs")

    return [
        # ── A. les prérequis du conteneur ────────────────────────────────
        A.ConteneurDemarre(),
        A.Protection(),
        A.Nesting(),
        A.Mp1Depot(),
        A.Mp2Sauvegardes(),
        A.Startup(),
        # Le seul endroit où le CT redémarre. Après tous les `pct set`, et
        # avant que quiconque ne regarde le montage.
        Barrier("prérequis du CT appliqués", "A"),

        # ── B. la pose dans le conteneur ─────────────────────────────────
        B.MontageVisible(),
        B.PaquetCT("sudo", "/usr/bin/sudo"),
        B.PaquetCT("python3-minimal", "/usr/bin/python3"),
        B.TimerFstrim(),
        B.ClusterDetecte(),
        B.SymlinkConf("10-homelab.conf", "conf.d"),
        B.SymlinkConf("pg_hba.conf"),
        B.FichierCT("pg-backup.service",
                    "/etc/systemd/system/pg-backup.service", 0o644),
        B.FichierCT("pg-backup.timer",
                    "/etc/systemd/system/pg-backup.timer", 0o644),
        B.FichierCT("pg-backup.sh", "/usr/local/bin/pg-backup.sh", 0o755),
        # `pgbk.sh` reste posé tant qu'un déploiement réel n'a pas prouvé le
        # moteur Python : c'est le filet du conteneur si `python3` manque.
        B.FichierCT("pgbk.sh", "/usr/local/bin/pgbk", 0o755),
        B.MoteurCT(),
        B.LanceurCT(),
        Barrier("unités du CT rechargées", "B"),
        B.TimerSauvegardeArme(),

        # ── D/E. l'outillage du nœud ─────────────────────────────────────
        D.ConfCtid(),
        D.Python3Hote(),
        D.PgbkHote(),
        D.PgHote(),
        D.PgtoolHote(),

        # ── G. la première sauvegarde, AVANT le hors-site ────────────────
        G.PremiereSauvegarde(),

        # ── E/F. la copie hors-site ──────────────────────────────────────
        D.Rclone(Path(rclone_bin)),
        F.CleGCP(Path(cle)),
        F.ConfigRclone(Path(rclone_conf), remote=remote, cle=Path(cle)),
        F.SourceHorsSite(),
        F.DropInNoeud(DROPIN_HORSSITE, node=_noeud()),
        F.UniteHorsSite("pgbk-offsite.sh", SCRIPT_HORSSITE, mode=0o755),
        F.UniteHorsSite("pgbk-offsite.service", UNITE_HORSSITE),
        F.UniteHorsSite("pgbk-offsite.timer", TIMER_HORSSITE),
        Barrier("unités du nœud rechargées", "F"),
        F.ArmementHorsSite(),

        # ── G. les opérations à secret, sur demande explicite ────────────
        G.RoleAdmin(),
        G.Locataire(),

        # ── C. les contrôles, en dernier ─────────────────────────────────
        C.HbaRules(),
        C.SocketsEnEcoute(),
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
    qu'on le sait. Cette séparation est ce qui permet à trois copies d'unité de
    ne provoquer qu'un seul rechargement, là où le bash levait un drapeau
    `copied` à la main à chaque appel.
    """
    ctx.on_effect(A.EFFET_REBOOT, _redemarrer_ct)
    ctx.on_effect(B.EFFET_DAEMON_RELOAD, _daemon_reload_ct)
    ctx.on_effect(B.EFFET_RESTART, _restart_postgresql)
    ctx.on_effect(B.EFFET_REFRESH, _reload_postgresql)
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
    à la première pose, quand `pg_hba` et le drop-in viennent d'apparaître."""
    Systemd(ctx.runner.for_container(ctx.opts.ctid)).restart("postgresql")
    # Le rechargement final n'a plus rien à apporter après un redémarrage.
    ctx.facts["postgresql_restarted"] = True


def _reload_postgresql(ctx: Context) -> None:
    if ctx.facts.get("postgresql_restarted"):
        return
    if ctx.opts.force_restart:
        _restart_postgresql(ctx)
        return
    Systemd(ctx.runner.for_container(ctx.opts.ctid)).reload("postgresql")


def _daemon_reload_hote(ctx: Context) -> None:
    Systemd(ctx.runner).daemon_reload()


# ─── le déploiement ──────────────────────────────────────────────────────────


def deployer(ctx: Context) -> list[Report]:
    """Un parcours, trois modes. Renvoie le bilan."""
    brancher_effets(ctx)
    return traverse(etapes(ctx), ctx, effets_finaux=EFFETS_FINAUX)
