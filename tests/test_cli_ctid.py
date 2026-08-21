"""`--ctid` doit s'accepter des deux côtés de la sous-commande.

Le bash prenait `pg-deploy.sh --ctid 201` : les drapeaux venaient après le nom
du script, il n'y avait pas de sous-commande. En passant à `pg deploy`,
`--ctid` s'est retrouvé sur le parseur GLOBAL, donc uniquement AVANT le verbe —
et toutes les invocations documentées, y compris celles de l'exercice de PRA
qui monte le CT 299, ont cessé de fonctionner sans que rien ne le signale.

La forme naturelle doit marcher. C'est la promesse d'une bascule franche : la
ligne de commande d'hier ne doit pas devenir fausse.
"""

from __future__ import annotations

import pytest

from pgtool.cli import build_parser


def _args(*argv):
    return build_parser().parse_args(list(argv))


def test_ctid_apres_le_verbe():
    """La forme documentée partout, et celle que le bash acceptait."""
    assert _args("deploy", "--ctid", "299").ctid == "299"


def test_ctid_avant_le_verbe():
    """La forme que le parseur global impose. Elle doit rester valide : le
    README l'emploie pour les commandes du moteur."""
    assert _args("--ctid", "299", "list").ctid == "299"


def test_ctid_avant_le_verbe_nest_PAS_ecrase_par_le_defaut_du_sous_parseur():
    """Le piège d'argparse : un même `dest` défini des deux côtés fait que le
    défaut du sous-parseur écrase la valeur déjà analysée. On obtiendrait
    `None`, donc le CTID du fichier de configuration — c'est-à-dire la
    PRODUCTION, alors que l'utilisateur en visait un autre."""
    assert _args("--ctid", "299", "deploy").ctid == "299"


def test_ctid_apres_le_verbe_lemporte():
    """Le plus proche de la commande gagne : c'est ce qu'on lit en dernier."""
    assert _args("--ctid", "200", "deploy", "--ctid", "299").ctid == "299"


def test_sans_ctid_la_valeur_reste_absente():
    """Aucun défaut inventé ici : c'est `resolve_ctid` qui tranche, avec
    l'environnement puis le fichier."""
    assert _args("deploy").ctid is None
    assert _args("list").ctid is None


@pytest.mark.parametrize(
    "argv",
    [
        ("deploy",), ("status",), ("list",), ("backup",), ("offsite",),
        ("show",), ("verify", "forgejo"), ("restore", "forgejo"),
        ("delete", "20260821-023639"),
    ],
)
def test_toutes_les_sous_commandes_acceptent_ctid(argv):
    """Un ordre qui marche pour l'une et pas pour l'autre est une frontière
    qu'aucun message n'explique."""
    assert _args(*argv, "--ctid", "299").ctid == "299"


def test_les_invocations_de_lexercice_de_pra_sanalysent():
    """Elles sont écrites noir sur blanc dans le document, et c'est sur elles
    que reposent les deux garde-fous du CT jetable."""
    a = _args("deploy", "--ctid", "299", "--no-offsite", "--dry-run")
    assert (a.ctid, a.no_offsite, a.dry_run) == ("299", True, True)
    assert _args("deploy", "--ctid", "200").ctid == "200"
