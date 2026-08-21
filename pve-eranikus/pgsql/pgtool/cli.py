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
            print(render_show(store, args.instantane or "latest"))
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
        vise = delegue.plan(commande, ct_args)
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

    delegue.hand_over(commande, ct_args, yes=oui)
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

    offsite = sous.add_parser(
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

    for nom, positionnels in DELEGUEES.items():
        p = sous.add_parser(
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
