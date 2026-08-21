"""`pg` — point d'entrée unique de l'outillage PostgreSQL.

Là où il y avait trois commandes (`pg-deploy.sh`, `pgbk`, `pgbk-offsite`), il
n'y en a qu'une. Les sous-commandes arrivent au fil de la migration ; seule
`offsite` est portée à ce stade.

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pg",
        description="Outillage du cluster PostgreSQL mutualisé.",
        epilog="Documentation : pve-eranikus/pgsql/README.md et doc/RUNBOOK.md",
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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    signal.signal(signal.SIGINT, _quitter_sur_signal)
    signal.signal(signal.SIGTERM, _quitter_sur_signal)

    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return args.fonction(args)
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
        error("copie hors-site NON garantie pour cette exécution")
        return 1
