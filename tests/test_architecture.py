"""Les règles d'architecture, vérifiées mécaniquement.

Une convention qu'aucun test ne défend se perd à la troisième modification.
Chacune de celles-ci est un critère d'acceptation du plan de migration.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"
CORE = LIB / "core"
PROXMOX = LIB / "proxmox"

# Les règles génériques valent aussi pour l'outillage d'un service : c'est du
# code de production, poussé sur les mêmes machines.
#
# Un service = un répertoire, un paquet d'outillage, un lanceur. Inscrire un
# service ici est le geste qui le place sous la surveillance de TOUTES les
# règles ci-dessous ; l'oublier laisserait son code hors de tout contrôle, et
# aucun autre test ne le rattraperait.
SERVICES = [
    (REPO / "pve-eranikus" / "pgsql", "pgtool", "pg"),
    (REPO / "pve-ysera" / "forgejo", "fjtool", "fj"),
]

OUTILS = [
    chemin
    for racine, paquet, _ in SERVICES
    for chemin in sorted((racine / paquet).rglob("*.py"))
]
SOURCES = sorted(LIB.rglob("*.py")) + OUTILS
LANCEURS = [racine / lanceur for racine, _, lanceur in SERVICES]


def _relatif(chemin: Path) -> str:
    return str(chemin.relative_to(REPO))

# Ce que la bibliothèque standard fournit et que ce code utilise. Toute entrée
# supplémentaire devrait être un ajout délibéré, pas une dérive.
STDLIB = set(sys.stdlib_module_names)


def _modules_importes(chemin: Path) -> set[str]:
    arbre = ast.parse(chemin.read_text(encoding="utf-8"))
    noms: set[str] = set()
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Import):
            noms.update(alias.name.split(".")[0] for alias in noeud.names)
        elif isinstance(noeud, ast.ImportFrom):
            if noeud.level:  # import relatif : interne au paquet
                continue
            if noeud.module:
                noms.add(noeud.module.split(".")[0])
    return noms


def test_il_y_a_bien_des_sources_a_verifier():
    assert SOURCES, "aucun fichier trouvé sous lib/ — le test ne vérifie rien"


@pytest.mark.parametrize("chemin", SOURCES, ids=_relatif)
def test_bibliotheque_standard_uniquement(chemin):
    """Aucun pip install sur l'hyperviseur ni dans un conteneur."""
    interne = {"core", "proxmox"} | {paquet for _, paquet, _ in SERVICES}
    externes = _modules_importes(chemin) - STDLIB - interne
    assert not externes, f"{chemin.name} importe hors stdlib : {sorted(externes)}"


@pytest.mark.parametrize(
    "chemin", sorted(CORE.rglob("*.py")), ids=_relatif
)
def test_core_nimporte_jamais_proxmox(chemin):
    """core est le seul paquet poussé dans les conteneurs, où `pct` n'existe
    pas. L'y faire dépendre casserait le moteur là où il doit tourner."""
    assert "proxmox" not in _modules_importes(chemin)


@pytest.mark.parametrize("chemin", sorted(LIB.rglob("*.py")), ids=_relatif)
def test_aucun_nom_de_service_dans_lib(chemin):
    """« Si "postgres" apparaît dans lib/, le code est au mauvais endroit. »

    Les noms d'outils génériques (psql, pg_dump) sont admis : ce sont des
    binaires, pas des services de ce homelab.
    """
    texte = chemin.read_text(encoding="utf-8").lower()
    for interdit in ("forgejo", "adguard", "traefik", "eranikus", "ysera", "homepage"):
        assert interdit not in texte, f"{chemin.name} nomme « {interdit} »"


@pytest.mark.parametrize("chemin", SOURCES, ids=_relatif)
def test_jamais_de_shell(chemin):
    """Le triple échappement Python → pct → shell du conteneur n'existe pas,
    parce qu'aucun shell n'intervient."""
    arbre = ast.parse(chemin.read_text(encoding="utf-8"))
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.keyword) and noeud.arg == "shell":
            valeur = getattr(noeud.value, "value", None)
            assert valeur is False, f"{chemin.name} : shell=True"
        if isinstance(noeud, ast.Attribute) and noeud.attr in {"system", "popen"}:
            base = getattr(noeud.value, "id", "")
            assert base != "os", f"{chemin.name} : os.{noeud.attr}"


@pytest.mark.parametrize("chemin", SOURCES, ids=_relatif)
def test_pas_de_commande_construite_par_concatenation(chemin):
    """Un argv est une liste d'arguments, jamais une phrase.

    On cherche les appels à `subprocess.*` dont un argument est le résultat
    d'une concaténation ou d'un f-string : c'est la forme qui réintroduit un
    shell sans le dire.
    """
    arbre = ast.parse(chemin.read_text(encoding="utf-8"))
    for noeud in ast.walk(arbre):
        if not isinstance(noeud, ast.Call):
            continue
        cible = noeud.func
        if not (isinstance(cible, ast.Attribute) and
                getattr(cible.value, "id", "") == "subprocess"):
            continue
        for arg in noeud.args:
            assert not isinstance(arg, (ast.JoinedStr, ast.BinOp)), (
                f"{chemin.name} : argv construit par concaténation"
            )


def test_tout_compile():
    """Critère d'acceptation : python3 -m compileall passe."""
    res = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", str(LIB)],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_core_simporte_sans_proxmox(tmp_path):
    """Invariant du conteneur : il ne reçoit QUE `core/`.

    On reconstruit cette situation-là — un chemin d'import où `proxmox`
    n'existe pas du tout — et on importe pour de vrai dans un sous-processus.
    Se contenter de `lib/` sur le chemin ne prouverait rien : `proxmox` y est,
    et un import oublié passerait inaperçu.
    """
    faux_ct = tmp_path / "usr-local-lib"
    faux_ct.mkdir()
    (faux_ct / "core").symlink_to(CORE, target_is_directory=True)

    # -I isole du site utilisateur ET de PYTHONPATH : le chemin est donc
    # inséré depuis le code, ce qui ne laisse que la stdlib et « core ».
    code = (
        f"import sys; sys.path.insert(0, {str(faux_ct)!r}); "
        "import importlib.util as u; "
        "assert u.find_spec('proxmox') is None, 'proxmox ne devrait pas être visible'; "
        "import core, core.log, core.runner, core.commands; "
        "print('ok')"
    )
    res = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.strip() == "ok"


@pytest.mark.parametrize(
    "chemin,paquet",
    [(racine / lanceur, paquet) for racine, paquet, lanceur in SERVICES],
    ids=[lanceur for _, _, lanceur in SERVICES],
)
def test_le_lanceur_verifie_la_version_avant_tout(chemin, paquet):
    """Le contrôle de version doit précéder l'import du reste : le message de
    refus doit pouvoir s'afficher là où le reste ne s'analyserait pas."""
    texte = chemin.read_text(encoding="utf-8")
    assert texte.startswith("#!/usr/bin/python3"), (
        "chemin absolu de l'interpréteur : le PATH de systemd et de pct exec "
        "est minimal"
    )
    assert texte.index("require_python()") < texte.index(f"from {paquet}")
    ast.parse(texte)


@pytest.mark.parametrize("chemin", LANCEURS, ids=_relatif)
def test_le_lanceur_est_executable(chemin):
    """Un lanceur non exécutable ne se voit qu'à l'usage.

    Le déploiement le pousse en 0755 dans le conteneur, donc la production
    survit ; mais « ./fj » joué depuis le dépôt — la façon dont on compare une
    commande à celle qu'elle remplace — échoue en « Permission denied », sans
    rapport visible avec un bit perdu au commit.
    """
    assert chemin.stat().st_mode & 0o111, f"{chemin} n'est pas exécutable"


def test_pytest_nest_jamais_importe_par_la_production():
    """pytest est admis en développement, jamais importé par le code livré."""
    for chemin in SOURCES:
        assert "pytest" not in _modules_importes(chemin), chemin.name


@pytest.mark.parametrize(
    "racine,paquet,modules",
    [
        (REPO / "pve-eranikus" / "pgsql", "pgtool",
         "pgtool.cli, pgtool.engine, pgtool.snapshots, pgtool.restore"),
        (REPO / "pve-ysera" / "forgejo", "fjtool",
         "fjtool.cli, fjtool.backup, fjtool.version"),
    ],
    ids=["pgtool", "fjtool"],
)
def test_la_charge_utile_du_conteneur_simporte_seule(
    tmp_path, racine, paquet, modules
):
    """Ce que `pct push` dépose dans le CT : `core/` et `pgtool/`, jamais
    `proxmox/`. Le moteur doit s'importer entièrement dans ces conditions —
    sinon il échoue dans le seul endroit où il est censé tourner.

    C'est aussi ce qui impose les imports paresseux de `cli.py`.
    """
    faux_ct = tmp_path / f"usr-local-lib-{paquet}"
    faux_ct.mkdir()
    (faux_ct / "core").symlink_to(CORE, target_is_directory=True)
    (faux_ct / paquet).symlink_to(racine / paquet, target_is_directory=True)

    code = (
        f"import sys; sys.path.insert(0, {str(faux_ct)!r}); "
        "import importlib.util as u; "
        "assert u.find_spec('proxmox') is None, 'proxmox ne doit pas être dans le CT'; "
        f"import {modules}; "
        "print('ok')"
    )
    res = subprocess.run([sys.executable, "-I", "-c", code],
                         capture_output=True, text=True, cwd=str(tmp_path))
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.strip() == "ok"
