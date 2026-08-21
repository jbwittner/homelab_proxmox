"""`pg` — point d'entrée unique de l'outillage PostgreSQL.

Là où il y avait trois commandes (`pg-deploy.sh`, `pgbk`, `pgbk-offsite`), il
n'y en a qu'une. Les sous-commandes arrivent au fil de la migration.

Les sous-commandes sont importées **paresseusement**, au moment de la
répartition. C'est un invariant du conteneur : il ne reçoit que `core/` et
`pgtool/`, jamais `proxmox/`, et un import en tête de fichier ferait échouer
`pg list` dans le CT sur un `ImportError` sans rapport avec ce qu'on demande.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from typing import Sequence

from core.log import error

# Commandes acheminées vers le moteur du conteneur, avec leurs positionnels.
# « ? » = facultatif. L'ordre est celui de la ligne de commande.
DELEGUEES: dict[str, list[tuple[str, bool]]] = {
    "backup": [],
    "list": [],
    "show": [("instantane", True)],
    "restore": [("base", False), ("instantane", True)],
    "verify": [("base", False)],
    "delete": [("instantane", False)],
}

AIDE = {
    "backup": "déclenche une sauvegarde immédiate",
    "list": "instantanés disponibles : âge, taille, bases",
    "show": "manifeste et fichiers d'un instantané (défaut : latest)",
    "restore": "restaure une base depuis un instantané — ÉCRASE la base visée",
    "verify": "contrôle les ACL et les propriétaires d'une base",
    "delete": "supprime un instantané — jamais le dernier",
}


class Parser(argparse.ArgumentParser):
    """Un usage fautif sort en 1, pas en 2.

    `argparse` sort en 2 sur une erreur d'arguments. Dans la table de cette
    commande, 2 veut dire « au moins un transfert a échoué » : une faute de
    frappe serait consignée par systemd comme une panne de transfert, et se
    lirait comme telle trois semaines plus tard. Une erreur d'usage appartient
    à la famille « environnement inutilisable », soit 1 — c'est aussi ce que
    faisaient les scripts bash.

    `--help` continue de sortir en 0 : ce n'est pas une erreur.
    """

    def error(self, message: str):  # noqa: D102 - contrat d'argparse
        self.print_usage(sys.stderr)
        error(f"{self.prog} : {message}")
        raise SystemExit(1)


def _quitter_sur_signal(numero, _cadre):  # pragma: no cover - dépend du signal
    """130, pour SIGINT comme pour SIGTERM.

    Python ne rend 130 de lui-même que pour KeyboardInterrupt, et SIGTERM tue
    le processus par signal sans jamais produire de code. Les deux sont donc
    interceptés explicitement, sinon le contrat de codes de retour ne tient
    que pour la moitié des interruptions.
    """
    error(f"interrompu par signal ({signal.Signals(numero).name})")
    raise SystemExit(130)


def _offsite(args: argparse.Namespace) -> int:
    import os

    from core.runner import Runner
    from pgtool.offsite import OffsiteConfig, run

    runner = Runner()
    hostname = os.uname().nodename.split(".")[0]
    cfg = OffsiteConfig.from_env(os.environ, hostname=hostname)
    return run(cfg, runner, dry_run=args.dry_run, now=time.time())


# ─── pg status ───────────────────────────────────────────────────────────────


def _status(args: argparse.Namespace) -> int:
    """Les trois maillons du montage, regardés ENSEMBLE.

    `pg deploy --status` dit si les fichiers sont en place ; celui-ci dit si le
    montage fonctionne. Ce sont deux questions différentes, et la seconde est
    celle qui manquait : un timer armé qui échoue chaque nuit reste armé.
    """
    import os

    from core.log import error, info, step, warn
    from core.runner import Runner
    from pgtool.deploy import Options, Paths
    from pgtool.etat import alarmes, code_de_sortie, relever, render_etat
    from pgtool.location import Refus, read_conf, resolve_ctid

    if os.geteuid() != 0:
        raise Refus("à lancer en root sur le nœud (pct l'exige)")

    runner = Runner()
    ctid = resolve_ctid(flag=args.ctid, env=os.environ, conf=read_conf())

    from types import SimpleNamespace

    ctx = SimpleNamespace(
        runner=runner,
        opts=Options(ctid=ctid, do_offsite=not args.no_offsite),
    )
    etat = relever(ctx)

    # Le tableau est une DONNÉE : il se recopie tel quel, sans horodatage. Les
    # alarmes sont des messages sur cette donnée, elles passent donc par la
    # journalisation — la distinction posée dans core.log.
    print(render_etat(etat))
    dits = alarmes(etat)
    if not dits:
        print()
        step("tout est en ligne")
        return 0
    print()
    for ligne in dits:
        warn(ligne)
    return code_de_sortie(etat)


# ─── pg deploy ───────────────────────────────────────────────────────────────

# CTID par défaut, et le seul de tout l'outillage. `pg deploy` doit pouvoir
# amorcer une installation vierge, quand `/etc/default/pgbk` n'existe pas
# encore — c'est lui qui l'écrit. Les autres commandes, elles, refusent de
# deviner : restaurer dans un conteneur supposé n'a pas d'excuse.
CTID_PAR_DEFAUT = 200


def _mode_de(args: argparse.Namespace):
    """`--status` constate, `--dry-run` constate ET annonce ce qu'il ferait.

    Les confondre ferait passer le premier pour un plan, alors qu'il ne dit
    rien de ce qu'il faudrait faire.
    """
    from core.converge import Mode

    if args.status:
        return Mode.STATUS
    if args.dry_run:
        return Mode.DRY_RUN
    return Mode.APPLY


def _options_de(args: argparse.Namespace, *, ctid: int, env=None):
    """Les drapeaux, plus les trois réglages du volume de sauvegarde.

    Ceux-là viennent de l'environnement et non de la ligne de commande : ils ne
    servent qu'à la CRÉATION du volume, c'est-à-dire une fois dans la vie d'un
    conteneur. En faire des drapeaux les mettrait sur le même plan que
    `--no-offsite`, alors qu'ils ne se rejouent jamais — après la création,
    agrandir est un geste séparé (`pct resize`).

    C'est aussi ce dont l'exercice de PRA a besoin pour monter un CT jetable
    sans lui réserver 50 Go.
    """
    import os

    from pgtool.deploy import Options

    env = os.environ if env is None else env
    return Options(
        ctid=ctid,
        mp2_mount=env.get("PG_MP2_MOUNT") or Options.mp2_mount,
        mp2_storage=env.get("PG_MP2_STORAGE") or Options.mp2_storage,
        mp2_size=_entier(env.get("PG_MP2_SIZE"), Options.mp2_size,
                         "PG_MP2_SIZE"),
        do_container=not args.no_container,
        do_offsite=not args.no_offsite,
        do_install=not args.no_install,
        do_first_run=not args.no_first_run,
        force_restart=args.restart,
        admin=args.admin,
        tenant=args.tenant,
    )


def _entier(brut: str | None, defaut: int, nom: str) -> int:
    """Refuse plutôt que de retomber en silence sur le défaut.

    Une valeur illisible ignorée créerait un volume qu'on n'a pas demandé — et
    un volume ne se redimensionne pas d'un déploiement.
    """
    from pgtool.location import Refus

    if not brut:
        return defaut
    try:
        return int(brut)
    except ValueError:
        raise Refus(f"{nom} n'est pas un entier : {brut}") from None


def _secrets_autorises(opts) -> bool:
    """Un mot de passe n'apparaît que si on l'a demandé.

    Le moteur refuse toute action marquée `generates_secret` sans cette
    autorisation ; c'est la règle d'AGENTS.md devenue propriété du parcours,
    et non une discipline de relecture.
    """
    return bool(opts.admin or opts.tenant)


def _source_du_depot(args: argparse.Namespace):
    """Où lire ce qu'on va poser. Le dépôt, jamais la copie installée.

    Poser depuis `/usr/local/lib/pgtool` reviendrait à redéployer ce qui est
    déjà là : le déploiement n'aurait plus aucune source de vérité, et un
    `git pull` cesserait d'avoir le moindre effet.
    """
    from pathlib import Path

    from pgtool.location import Refus

    if getattr(args, "src", None):
        return Path(args.src)

    racine = Path(__file__).resolve().parents[1]
    if (racine / "ct").is_dir():
        return racine
    raise Refus(
        "pg deploy se lance depuis le dépôt (…/pgsql/pg), pas depuis la "
        "copie installée — ou passer --src <chemin du service>"
    )


def _contexte_deploy(args: argparse.Namespace, *, ctid: int, runner, src):
    from pgtool.deploy import Paths, contexte

    opts = _options_de(args, ctid=ctid)
    return contexte(
        runner=runner,
        paths=Paths(src=src),
        opts=opts,
        mode=_mode_de(args),
        allow_secrets=_secrets_autorises(opts),
    )


def _code_de_sortie(rapports) -> int:
    """0 si rien n'a échoué. « Bloquée » n'est pas un échec.

    Une étape bloquée est une étape NON DEMANDÉE — sortir en 1 ferait passer
    un déploiement de routine pour un incident, et systemd le consignerait
    comme tel.
    """
    from core.converge import BLOCKED, SKIP

    for rapport in rapports:
        if rapport.state in (SKIP, BLOCKED, "ok", "drift", "absent"):
            continue
        return 1
    return 0


def _deploy(args: argparse.Namespace) -> int:
    """Un parcours, trois modes, un bilan.

    Le bash refaisait quarante-quatre fois le triplet « constater / annoncer /
    appliquer ». Ici il n'y a qu'un appel, et l'ordre des étapes est une donnée
    que l'on peut relire.
    """
    import os

    from core.converge import Mode, render_report, render_summary
    from core.log import info, step
    from core.runner import Runner
    from pgtool.location import Refus, read_conf, resolve_ctid
    from pgtool.plan import deployer

    if os.geteuid() != 0:
        raise Refus("à lancer en root sur le nœud (pct l'exige)")

    src = _source_du_depot(args)
    ctid = resolve_ctid(
        flag=args.ctid, env=os.environ, conf=read_conf(),
        defaut=CTID_PAR_DEFAUT,
    )
    ctx = _contexte_deploy(args, ctid=ctid, runner=Runner(), src=src)

    step(f"CT {ctid} — dépôt {src} (mp1 : ct/, hôte : host/)")
    if ctx.mode is not Mode.APPLY:
        info(f"(mode --{ctx.mode.value} : aucune modification)")

    rapports = deployer(ctx)

    step("Résumé")
    rendu = render_report(rapports) if ctx.mode is Mode.STATUS \
        else render_summary(rapports)
    for ligne in rendu.splitlines():
        info(ligne)
    if ctx.mode is not Mode.APPLY:
        info("Aucune modification appliquée.")
    return _code_de_sortie(rapports)


def _positionnels(args: argparse.Namespace, commande: str) -> list[str]:
    """Reconstitue les arguments dans l'ordre, en s'arrêtant au premier absent.

    Un positionnel facultatif non fourni ne doit pas laisser de trou : le
    moteur lit `$1` et `$2`, pas des options nommées.
    """
    sortie: list[str] = []
    for nom, _facultatif in DELEGUEES[commande]:
        valeur = getattr(args, nom, None)
        if valeur is None:
            break
        sortie.append(valeur)
    return sortie


def _deleguer(args: argparse.Namespace) -> int:
    """Répartition selon l'endroit : le nœud achemine, le conteneur travaille.

    Un seul fichier, deux rôles, et c'est la présence de `pct` qui tranche.
    """
    from core.runner import Runner
    from pgtool.location import Where, detect

    runner = Runner()
    if detect(runner) is Where.CONTAINER:
        return _moteur(args, runner)
    return _acheminer(args, runner)


def _moteur(args: argparse.Namespace, runner) -> int:
    """Mode moteur : on est DANS le conteneur, on fait le travail.

    Aucune trace de Proxmox ici — on ne voit que le dépôt de sauvegardes, un
    cluster sur sa socket locale et systemd. Les imports sont locaux à la
    fonction : `pgtool` est poussé dans le CT sans `proxmox`, et un import en
    tête de module ferait échouer la commande sur un manque sans rapport.
    """
    import os
    import time
    from pathlib import Path

    from core.commands import Psql, Systemd
    from core.log import detail, info, step, warn
    from pgtool.engine import (
        DeleteRefused,
        describe_delete,
        do_delete,
        list_summary,
        plan_delete,
        render_list,
        render_show,
        show_anomalies,
    )
    from pgtool.location import Refus, confirm
    from pgtool.restore import RestoreError, restore, verify
    from pgtool.snapshots import Store

    if os.geteuid() != 0:
        raise Refus(
            "à lancer en root : « pg » depuis le nœud, "
            "ou dans le CT après « pct enter »"
        )

    dest = Path(os.environ.get("PG_BACKUP_DEST", "/var/backups/postgresql"))
    store = Store(dest)
    psql = Psql(runner)
    commande = args.commande

    try:
        if commande == "list":
            print(render_list(store, maintenant=time.time()))
            if store.snapshots():
                print()
                info(list_summary(store))
            return 0

        if commande == "show":
            reference = args.instantane or "latest"
            print(render_show(store, reference))
            for anomalie in show_anomalies(store.resolve(reference)):
                warn(f"  {anomalie}")
            return 0

        if commande == "backup":
            step("déclenchement de pg-backup.service")
            systemd = Systemd(runner)
            try:
                systemd.start("pg-backup.service")
            except Exception:
                for ligne in systemd.journal("pg-backup", lines=20):
                    warn(ligne)
                raise Refus("la sauvegarde a échoué — voir le journal ci-dessus")
            detail("\n".join(systemd.journal("pg-backup", lines=12)))
            return 0

        if commande == "verify":
            verify(psql, database=args.base)
            return 0

        if commande == "restore":
            if not args.yes:
                # Atteignable seulement depuis « pct enter » : le nœud, lui,
                # pose la question là où il y a un terminal et passe --yes.
                confirm(f"ÉCRASE la base {args.base}", args.base,
                        "le nom de la base")
            rapport = restore(psql, runner, store, database=args.base,
                              ref=args.instantane or "latest")
            verify(psql, database=rapport.database, owner=rapport.owner)
            return 0

        if commande == "delete":
            vise = plan_delete(store, args.instantane)
            if args.plan:
                # Contrat : la sortie standard ne porte QUE le nom résolu, le
                # nœud la lit pour formuler sa question.
                print(describe_delete(vise), file=sys.stderr, flush=True)
                print(vise.name)
                return 0
            if not args.yes:
                confirm(f"SUPPRIME l'instantané {vise.name}", vise.name,
                        "son nom")
            do_delete(store, vise)
            return 0

    except (DeleteRefused, RestoreError) as refus:
        raise Refus(str(refus)) from refus
    except LookupError as absent:
        raise Refus(str(absent)) from absent

    raise Refus(f"commande non portée dans le conteneur : {commande}")


def _acheminer(args: argparse.Namespace, runner) -> int:
    """Mode hôte : on achemine, le conteneur travaille.

    Toute la valeur ajoutée est ici — les gardes, la résolution du CTID et les
    confirmations — parce que ce sont les seules choses qu'on ne puisse pas
    faire de l'autre côté du montage.
    """
    import os

    from pgtool.location import (
        Delegate,
        Refus,
        confirm,
        read_conf,
        resolve_ctid,
    )

    commande = args.commande

    if os.geteuid() != 0:
        raise Refus("à lancer en root sur le nœud (pct l'exige)")

    ctid = resolve_ctid(flag=args.ctid, env=os.environ, conf=read_conf())
    delegue = Delegate(runner, ctid)
    delegue.preflight()

    ct_args = _positionnels(args, commande)
    oui = getattr(args, "yes", False)

    if commande == "delete":
        # Le conteneur seul sait à quoi une référence correspond : « 20260819 »
        # désigne la plus récente de ce jour, qui peut être le dernier
        # instantané. --plan applique toutes les gardes et n'efface rien.
        #
        # Le MÊME environnement que la suppression : résoudre contre un dépôt
        # et effacer dans un autre désignerait un instantané et en supprimerait
        # un second.
        vise = delegue.plan(commande, ct_args, env=os.environ)
        if not vise:
            raise Refus("rien à supprimer")
        if args.plan:
            # Le bash, lui, enchaînait sur la suppression : « --plan » n'y
            # était honnête que dans le conteneur. Ici il s'arrête, ce que son
            # nom promet.
            print(vise)
            return 0
        if not oui:
            confirm(
                f"SUPPRIME l'instantané {vise} du CT {ctid}", vise, "son nom"
            )
            oui = True

    elif commande == "restore" and not oui:
        confirm(
            f"ÉCRASE la base {args.base} du CT {ctid}",
            args.base,
            "le nom de la base",
        )
        oui = True

    # `pct exec` n'hérite d'aucun environnement : ce qui doit traverser la
    # frontière est transmis explicitement, sans quoi une variable posée ici
    # serait perdue en silence et la commande viserait le dépôt par défaut.
    delegue.hand_over(commande, ct_args, yes=oui, env=os.environ)
    return 0  # inatteignable : hand_over remplace le processus


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(
        prog="pg",
        description="Outillage du cluster PostgreSQL mutualisé.",
        epilog="Documentation : pve-eranikus/pgsql/README.md et doc/RUNBOOK.md",
    )
    parser.add_argument(
        "--ctid",
        metavar="ID",
        help=(
            "vise un autre conteneur pour cette commande, sans toucher à "
            "/etc/default/pgbk"
        ),
    )
    sous = parser.add_subparsers(dest="commande", required=True)

    def ajouter(nom: str, **kw):
        """Un sous-parseur qui accepte `--ctid` APRÈS le verbe, lui aussi.

        Le bash prenait « pg-deploy.sh --ctid 201 » : il n'y avait pas de
        sous-commande, les drapeaux venaient après le nom du script. Sans ce
        doublon, `--ctid` n'existerait qu'AVANT le verbe et toutes les
        invocations documentées — dont celles de l'exercice de PRA qui monte
        le CT 299 — auraient cessé de fonctionner.

        `SUPPRESS` est la clé : sans lui, le défaut du sous-parseur écraserait
        la valeur déjà analysée par le parseur global, et « pg --ctid 299
        deploy » viserait la PRODUCTION en silence. Avec lui, l'attribut n'est
        posé que s'il a été tapé — donc le plus proche de la commande gagne.
        """
        p = sous.add_parser(nom, **kw)
        p.add_argument("--ctid", metavar="ID", default=argparse.SUPPRESS,
                       help=argparse.SUPPRESS)
        return p

    offsite = ajouter(
        "offsite",
        help="copie les instantanés absents du bucket distant",
        description=(
            "Copie hors-site vers GCS. Tourne sur l'hôte, jamais dans le CT. "
            "copy, jamais sync : la rétention distante est une règle de cycle "
            "de vie du bucket."
        ),
        epilog=(
            "Codes de retour : 0 tout en ligne | 1 environnement inutilisable "
            "| 2 transfert en échec | 3 objet distant divergent, intervention "
            "humaine | 130 interrompu."
        ),
    )
    offsite.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "n'écrit aucun objet. Le contrôle de divergence est tout de même "
            "joué sur les instantanés complets — c'est une lecture."
        ),
    )
    offsite.set_defaults(fonction=_offsite)

    etat = ajouter(
        "status",
        help="l'état du montage : sauvegardes, timers des deux côtés, hors-site",
        description=(
            "Constate, ne modifie rien. « pg deploy --status » dit si les "
            "fichiers sont en place ; celui-ci dit si le montage fonctionne."
        ),
        epilog=(
            "Codes de retour : 0 tout en ligne | 1 au moins une alarme. Un "
            "maillon non constaté est une alarme, pas un silence."
        ),
    )
    etat.add_argument(
        "--no-offsite", action="store_true",
        help="n'interroge pas le bucket (utile hors ligne)",
    )
    etat.set_defaults(fonction=_status)

    deploy = ajouter(
        "deploy",
        help="pose et vérifie tout le montage : conteneur, nœud, hors-site",
        description=(
            "Convergence complète, sur le nœud. Le plan est produit par le "
            "constat : ce que --dry-run annonce est exactement ce que le mode "
            "réel exécute, il n'y en a pas deux descriptions."
        ),
        epilog=(
            "Un drapeau --no-* ne désactive jamais un contrôle, seulement une "
            "pose : le bilan reste complet quels que soient les drapeaux."
        ),
    )
    deploy.add_argument(
        "--status", action="store_true",
        help="constate et dresse le bilan avec ses motifs. N'écrit rien.",
    )
    deploy.add_argument(
        "--dry-run", action="store_true",
        help="constate ET annonce chaque modification. N'écrit rien.",
    )
    deploy.add_argument(
        "--restart", action="store_true",
        help=(
            "redémarre PostgreSQL même sans changement de configuration "
            "(listen_addresses ne se relit pas à chaud)"
        ),
    )
    deploy.add_argument(
        "--no-container", action="store_true",
        help=(
            "saute les PRÉREQUIS du conteneur — disques, protection, nesting. "
            "La pose dans le CT a lieu quand même. L'état de mp2 reste alors "
            "non déterminé, donc le hors-site ne s'armera pas."
        ),
    )
    deploy.add_argument(
        "--no-offsite", action="store_true",
        help="saute la copie hors-site et ses prérequis",
    )
    deploy.add_argument(
        "--no-install", action="store_true",
        help="n'installe aucun paquet ; un manque est constaté, pas comblé",
    )
    deploy.add_argument(
        "--no-first-run", action="store_true",
        help="ne déclenche pas la première sauvegarde",
    )
    deploy.add_argument(
        "--admin", metavar="ROLE",
        help=(
            "crée un compte d'administration s'il n'existe pas. AFFICHE UN "
            "MOT DE PASSE, une seule fois. Un rôle existant n'est jamais touché."
        ),
    )
    deploy.add_argument(
        "--tenant", metavar="NOM",
        help=(
            "crée une base et son rôle s'ils n'existent pas. AFFICHE UN MOT "
            "DE PASSE, une seule fois."
        ),
    )
    deploy.add_argument(
        "--src", metavar="CHEMIN",
        help=argparse.SUPPRESS,  # dépannage : le dépôt est trouvé tout seul
    )
    deploy.set_defaults(fonction=_deploy)

    for nom, positionnels in DELEGUEES.items():
        p = ajouter(
            nom,
            help=AIDE[nom],
            description=(
                f"{AIDE[nom].capitalize()}. Se tape sur le nœud : la commande "
                "est acheminée vers le moteur du conteneur."
            ),
        )
        for arg, facultatif in positionnels:
            p.add_argument(arg, nargs="?" if facultatif else None)
        if nom in ("restore", "delete"):
            p.add_argument(
                "--yes",
                action="store_true",
                help="saute la confirmation — pour un usage scripté, jamais à la main",
            )
        if nom == "delete":
            p.add_argument(
                "--plan",
                action="store_true",
                help=(
                    "affiche l'instantané réellement visé et s'arrête. "
                    "Toutes les gardes s'appliquent, rien n'est effacé."
                ),
            )
        p.set_defaults(fonction=_deleguer)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    signal.signal(signal.SIGINT, _quitter_sur_signal)
    signal.signal(signal.SIGTERM, _quitter_sur_signal)

    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])

    from pgtool.location import Refus

    try:
        return args.fonction(args)
    except Refus as refus:
        # Un refus argumenté : le message a déjà tout dit, pas de trace.
        for ligne in str(refus).splitlines():
            if ligne:
                error(ligne)
        return 1
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - dernier filet, volontaire
        # AGENTS.md impose un `trap ERR` qui consigne la panne. Ici : le type,
        # le message, et l'endroit — puis un code 1. Un incident imprévu
        # appartient à la famille « environnement inutilisable » ; laisser
        # échapper un code arbitraire, comme le faisait le `exit $rc` du bash,
        # casserait le contrat que systemd et les habitudes supposent.
        trace = exc.__traceback__
        while trace is not None and trace.tb_next is not None:
            trace = trace.tb_next
        ou = ""
        if trace is not None:
            cadre = trace.tb_frame
            ou = f" — {cadre.f_code.co_filename}:{trace.tb_lineno}"
        error(f"échec inattendu : {type(exc).__name__}: {exc}{ou}")
        return 1
