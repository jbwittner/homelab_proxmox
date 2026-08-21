"""L'environnement d'une unité, tel que systemd le donnera au processus.

Une commande lancée à la main n'hérite de rien. Pour dire ce que la copie
hors-site fera cette nuit, il faut lire ce que systemd lui passera — pas ce que
le shell courant contient, qui ne ressemble à rien de ce qui tournera à 3h30.
"""

from __future__ import annotations

from core.commands import Systemd
from core.runner import FakeRunner, Result


def _systemd(sortie: str) -> Systemd:
    runner = FakeRunner()
    runner.when(lambda argv: "show" in argv, Result(("x",), 0, sortie, ""))
    return Systemd(runner)


def test_lenvironnement_dune_unite_est_lu_tel_que_systemd_le_donnera():
    """`systemctl show -p Environment` rend une seule ligne d'affectations,
    drop-in compris — c'est justement le drop-in qui porte le nœud et le
    volume, donc l'unité seule ne suffirait pas."""
    env = _systemd(
        "PGBK_OFFSITE_NODE=pve-eranikus PGBK_OFFSITE_SRC=/data/subvol-200-disk-0"
    ).environment("pgbk-offsite.service")
    assert env["PGBK_OFFSITE_NODE"] == "pve-eranikus"
    assert env["PGBK_OFFSITE_SRC"] == "/data/subvol-200-disk-0"


def test_une_unite_sans_environnement_rend_un_dictionnaire_vide():
    """Vide, et non une exception : l'absence est un cas normal, pas une
    panne — l'appelant décidera de ses défauts."""
    assert _systemd("").environment("x.service") == {}


def test_un_fragment_sans_egal_est_ignore():
    """Ne pas laisser une ligne mal formée fabriquer une clé vide, qui
    écraserait ensuite une vraie valeur."""
    env = _systemd("A=1 pouet B=2").environment("x.service")
    assert env == {"A": "1", "B": "2"}
