"""Les accords entre fichiers, que rien d'autre ne vérifie.

Un chemin écrit à deux endroits finit par diverger, et la panne se découvre à
3h30 dans le journal. Ces tests-là relient le lanceur, le déployeur et l'unité
systemd : ils tombent le jour où l'un des trois bouge seul.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SERVICE = REPO / "pve-eranikus" / "pgsql"
LANCEUR = SERVICE / "pg"
DEPLOY = SERVICE / "pg-deploy.sh"
UNITE = SERVICE / "host" / "pgbk-offsite.service"

pytestmark = pytest.mark.skipif(
    not DEPLOY.exists(), reason="service absent de ce dépôt"
)


def _variable_bash(source: str, nom: str) -> str:
    m = re.search(rf"^{nom}=(\S+)", source, re.MULTILINE)
    assert m, f"{nom} introuvable dans pg-deploy.sh"
    return m.group(1)


def test_le_lanceur_et_le_deployeur_saccordent_sur_larbre_dimport():
    """`pg` cherche ses paquets là où `pg-deploy.sh` les pose. Si les deux
    divergent, `pg` ne démarre plus et rien ne l'annonce avant la première
    exécution du timer."""
    attendu = _variable_bash(DEPLOY.read_text(encoding="utf-8"), "HOST_LIB")
    assert f'"{attendu}"' in LANCEUR.read_text(encoding="utf-8")


def test_lunite_appelle_le_lanceur_la_ou_il_est_pose():
    """ExecStart doit désigner exactement le chemin d'installation, en absolu :
    le PATH de systemd n'inclut ni /usr/local/bin ni /usr/local/sbin."""
    attendu = _variable_bash(DEPLOY.read_text(encoding="utf-8"), "HOST_PG")
    ligne = [
        l for l in UNITE.read_text(encoding="utf-8").splitlines()
        if l.startswith("ExecStart=")
    ]
    assert ligne == [f"ExecStart={attendu} offsite"]
    assert attendu.startswith("/"), "chemin absolu obligatoire"


def test_lunite_garde_son_nom():
    """Le renommer orphelinerait le drop-in 10-noeud.conf, le lien
    d'activation et tout l'historique du journal."""
    assert UNITE.name == "pgbk-offsite.service"
    assert (UNITE.parent / "pgbk-offsite.timer").exists()


def test_les_lignes_environment_restent_analysables():
    """`pg-deploy.sh` les relit avec `awk -F=` : une par ligne, sans
    guillemets, sans `=` dans la valeur. C'est ce qui fait de l'unité la source
    de vérité des chemins hors-site."""
    for ligne in UNITE.read_text(encoding="utf-8").splitlines():
        if not ligne.startswith("Environment="):
            continue
        corps = ligne[len("Environment="):]
        assert corps.count("=") == 1, f"valeur ambiguë : {ligne}"
        assert '"' not in corps and "'" not in corps, f"guillemets : {ligne}"


def test_le_deployeur_verifie_la_presence_des_sources_python():
    """Refuser de démarrer sur un dépôt incomplet, plutôt que poser un arbre
    d'import à trous."""
    source = DEPLOY.read_text(encoding="utf-8")
    for attendu in ("$SRC/pg", "$SRC/pgtool/cli.py", "$LIB_SRC/core", "$LIB_SRC/proxmox"):
        assert f'"{attendu}"' in source, f"complétude non vérifiée : {attendu}"


def test_le_deployeur_exige_la_meme_version_de_python_que_le_lanceur():
    """Deux seuils différents donneraient un déploiement vert et un `pg` qui
    refuse de tourner."""
    from core import MIN_PYTHON

    source = DEPLOY.read_text(encoding="utf-8")
    majeur, mineur = MIN_PYTHON
    assert f"({majeur}, {mineur})" in source, (
        f"pg-deploy.sh doit contrôler python >= {majeur}.{mineur}"
    )


def test_lancien_script_reste_installe_le_temps_de_la_parite():
    """La bascule est franche pour l'unité, pas pour le dépôt : on garde de
    quoi comparer les deux sorties tant que la parité n'est pas constatée."""
    assert (SERVICE / "host" / "pgbk-offsite.sh").exists()
    assert "HOST_OFFSITE" in DEPLOY.read_text(encoding="utf-8")
