"""Le moteur, côté conteneur : inventaire, affichage, et les gardes de `delete`.

`delete` est la seule commande qui détruit. Ses gardes sont donc testées une
par une, et toutes portent sur un refus — aucun test de ce fichier ne supprime
quoi que ce soit pour prouver qu'une protection fonctionne.
"""

from __future__ import annotations

import os

import pytest

from pgtool.engine import (
    DeleteRefused,
    human_size,
    plan_delete,
    render_list,
    render_show,
)
from pgtool.snapshots import Store


def _snap(racine, nom, *, fichiers=("globals.sql", "forge.dump", "MANIFEST"),
          age_h=0, maintenant=1_800_000_000, taille=1024):
    d = racine / nom
    d.mkdir(parents=True)
    for f in fichiers:
        (d / f).write_text("x" * taille)
    quand = maintenant - age_h * 3600
    os.utime(d, (quand, quand))
    return d


@pytest.fixture
def store(tmp_path):
    dest = tmp_path / "postgresql"
    dest.mkdir()
    return Store(dest)


# ─── tailles lisibles ────────────────────────────────────────────────────────


def test_human_size_suit_du_h():
    """`du -h` : une décimale en dessous de 10, un entier au-delà."""
    assert human_size(0) == "0"
    assert human_size(1023) == "1023"
    assert human_size(1024) == "1.0K"
    assert human_size(3300) == "3.2K"
    assert human_size(12 * 1024) == "12K"
    assert human_size(1024 ** 2) == "1.0M"
    assert human_size(34 * 1024) == "34K"


def test_human_size_monte_dans_les_unites():
    assert human_size(5 * 1024 ** 3) == "5.0G"
    assert human_size(2 * 1024 ** 4) == "2.0T"


# ─── gardes de `delete` ──────────────────────────────────────────────────────


def test_latest_litteral_est_refuse(store):
    """Il n'y a rien à supprimer sous ce nom : `latest` est un lien, et sa
    cible est justement l'instantané protégé."""
    _snap(store.dest, "20260820-093240")
    with pytest.raises(DeleteRefused, match="protégé"):
        plan_delete(store, "latest")


def test_le_dernier_instantane_est_protege(store):
    """Supprimer la dernière sauvegarde laisserait le cluster sans filet."""
    _snap(store.dest, "20260819-233627")
    _snap(store.dest, "20260820-093240")
    with pytest.raises(DeleteRefused) as exc:
        plan_delete(store, "20260820-093240")
    assert "dernier instantané" in str(exc.value)
    assert "pg backup" in str(exc.value), "le message doit dire quoi faire"


def test_la_protection_suit_la_RESOLUTION_pas_la_frappe(store):
    """« 20260820 » désigne la plus récente de ce jour — qui peut être le
    dernier instantané. C'est tout l'intérêt de résoudre avant de juger."""
    _snap(store.dest, "20260819-233627")
    _snap(store.dest, "20260820-093240")
    with pytest.raises(DeleteRefused, match="dernier"):
        plan_delete(store, "20260820")


def test_un_instantane_ordinaire_est_visable(store):
    _snap(store.dest, "20260819-233627")
    _snap(store.dest, "20260820-093240")
    vise = plan_delete(store, "20260819-233627")
    assert vise.name == "20260819-233627"
    assert vise.path.is_dir(), "planifier ne supprime rien"


def test_une_date_resout_vers_la_plus_recente_du_jour(store):
    _snap(store.dest, "20260819-023318")
    _snap(store.dest, "20260819-234458")
    _snap(store.dest, "20260820-093240")
    assert plan_delete(store, "20260819").name == "20260819-234458"


def test_une_execution_en_cours_nest_pas_supprimable(store):
    """pg-backup.sh nettoie ses propres débris ; s'en mêler risquerait
    d'effacer une sauvegarde en cours d'écriture."""
    _snap(store.dest, "20260820-093240")
    _snap(store.dest, "20260820-100000.part")
    with pytest.raises((DeleteRefused, ValueError)):
        plan_delete(store, "20260820-100000.part")


def test_un_instantane_inexistant_est_refuse(store):
    _snap(store.dest, "20260820-093240")
    with pytest.raises((DeleteRefused, LookupError)):
        plan_delete(store, "20260101-000000")


def test_un_seul_instantane_est_toujours_le_dernier(store):
    """Le cas limite : il ne doit jamais y avoir zéro sauvegarde."""
    _snap(store.dest, "20260820-093240")
    with pytest.raises(DeleteRefused, match="dernier"):
        plan_delete(store, "20260820-093240")


def test_une_reference_fantaisiste_ne_touche_pas_au_disque(store):
    _snap(store.dest, "20260820-093240")
    with pytest.raises(ValueError):
        plan_delete(store, "../../etc")


# ─── affichage ───────────────────────────────────────────────────────────────


def test_la_liste_marque_le_dernier(store):
    _snap(store.dest, "20260819-233627", age_h=30)
    _snap(store.dest, "20260820-093240", age_h=2)
    lignes = render_list(store, maintenant=1_800_000_000).splitlines()
    assert "INSTANTANÉ" in lignes[0]
    # Le repère est sur la LIGNE de l'instantané, pas sur la dernière ligne de
    # la sortie : celle-ci porte le bilan, comme en bash.
    ligne_recente = [l for l in lignes if "20260820-093240" in l][0]
    ligne_ancienne = [l for l in lignes if "20260819-233627" in l][0]
    assert ligne_recente.endswith("← latest")
    assert "← latest" not in ligne_ancienne


def test_la_liste_donne_lage_en_jours(store):
    _snap(store.dest, "20260819-233627", age_h=30)
    _snap(store.dest, "20260820-093240", age_h=2)
    texte = render_list(store, maintenant=1_800_000_000)
    assert "1j" in texte and "0j" in texte


def test_la_liste_nomme_les_bases(store):
    _snap(store.dest, "20260820-093240",
          fichiers=("globals.sql", "forge.dump", "wiki.dump", "MANIFEST"))
    assert "forge wiki" in render_list(store, maintenant=1_800_000_000)


def test_la_liste_ignore_les_executions_en_cours(store):
    _snap(store.dest, "20260820-093240")
    _snap(store.dest, "20260820-100000.part")
    assert "100000" not in render_list(store, maintenant=1_800_000_000)


def test_une_liste_vide_le_dit(store):
    assert "aucune sauvegarde" in render_list(store, maintenant=1_800_000_000)


def test_show_affiche_le_manifeste_et_les_fichiers(store):
    d = _snap(store.dest, "20260820-093240")
    (d / "MANIFEST").write_text(
        "date        : 2026-08-20T09:32:40+02:00\n"
        "postgresql  : 18.6\n"
        "bases       : forge\n"
    )
    texte = render_show(store, "20260820-093240")
    assert "18.6" in texte
    assert "forge.dump" in texte
    assert "globals.sql" in texte


def test_show_sans_manifeste_le_signale(store):
    _snap(store.dest, "20260820-093240", fichiers=("forge.dump",))
    assert "MANIFEST" in render_show(store, "20260820-093240")
