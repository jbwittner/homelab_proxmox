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
    show_anomalies,
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
    """`du -h` : une décimale en dessous de 10, un entier au-delà.

    Valeurs relevées sur un vrai `du -sh --apparent-size`, et non supposées —
    la version précédente de ce test affirmait « 3300 → 3.2K », ce qui était
    mon hypothèse et non le comportement de du.
    """
    assert human_size(0) == "0"
    assert human_size(1023) == "1023"
    assert human_size(1024) == "1.0K"
    assert human_size(1024 ** 2) == "1.0M"


def test_human_size_arrondit_AU_DESSUS_comme_du():
    """`du -h` ne fait pas un arrondi au plus proche : il compte des unités
    ENTAMÉES. Un octet de plus fait changer d'affichage.

    Mesuré :  1025 → 1.1K   3300 → 3.3K   10240 → 10K   10241 → 11K
    """
    assert human_size(1025) == "1.1K"
    assert human_size(3300) == "3.3K"
    assert human_size(10 * 1024) == "10K"
    assert human_size(10 * 1024 + 1) == "11K"
    assert human_size(5_000_000) == "4.8M"


def test_human_size_sur_les_chiffres_de_la_production():
    """Constaté le 21 août 2026, à la comparaison des deux moteurs :

        bash   : 8 sauvegarde(s), 34K
        python : 8 sauvegarde(s), 33K

    34341 octets vus par `du` (inodes de répertoires et lien latest compris),
    34251 octets de fichiers vus par le modèle. Les deux valent 34K dès lors
    que l'arrondi est celui de du — le désaccord ne venait pas du périmètre.
    """
    assert human_size(34341) == "34K"
    assert human_size(34251) == "34K"


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


# ─── parité avec df et du ────────────────────────────────────────────────────


def test_lespace_libre_sarrondit_au_dessus_comme_df(monkeypatch):
    """`df -m` arrondit AU-DESSUS ; tronquer donne une unité de moins.

    Constaté en production le 21 août 2026, à la comparaison des deux moteurs
    sur le même dépôt :

        bash   : 8 sauvegarde(s), 34K — 51200 Mo libres
        python : 8 sauvegarde(s), 33K — 51199 Mo libres
    """
    import shutil as _shutil

    from pgtool import engine

    MIO = 1024 * 1024

    class Usage:
        def __init__(self, libre):
            self.total = 100 * MIO
            self.used = 0
            self.free = libre

    # 51199 Mo et un octet : df annoncerait 51200.
    monkeypatch.setattr(engine.shutil, "disk_usage",
                        lambda _p: Usage(51199 * MIO + 1))
    assert engine.free_mb(_shutil.__file__) == 51200

    # Un compte exact ne doit pas être gonflé pour autant.
    monkeypatch.setattr(engine.shutil, "disk_usage",
                        lambda _p: Usage(51200 * MIO))
    assert engine.free_mb(_shutil.__file__) == 51200


# ─── pg show : le mode et le propriétaire sont des faits de sécurité ─────────


def test_show_affiche_le_mode_et_le_proprietaire(store):
    """Les dumps doivent être en 600 et appartenir à postgres : `globals.sql`
    porte les empreintes SCRAM de TOUS les rôles du cluster. `pg show` est la
    commande avec laquelle on inspecte un instantané — l'information doit y
    être. Le `ls -l` du bash la donnait ; le premier portage l'avait perdue."""
    d = _snap(store.dest, "20260820-093240", fichiers=("forgejo.dump",))
    (d / "forgejo.dump").chmod(0o600)
    texte = render_show(store, "20260820-093240")
    assert "600" in texte
    import pwd

    assert pwd.getpwuid(os.getuid()).pw_name in texte


def test_show_trie_de_facon_lisible(store):
    """MANIFEST en tête parce que « M » majuscule précède « f » minuscule
    serait un artefact d'encodage, pas un choix.

    On lit la SEULE section des fichiers : la phrase « pas de MANIFEST »
    contient elle aussi le mot, et un filtre trop large la ramasserait — elle
    l'a fait à la première écriture de ce test.
    """
    _snap(store.dest, "20260820-093240",
          fichiers=("MANIFEST", "forgejo.dump", "globals.sql"))
    lignes = render_show(store, "20260820-093240").splitlines()
    debut = lignes.index("fichiers :")
    noms = [l.split()[0] for l in lignes[debut + 1:]]
    assert noms == ["forgejo.dump", "globals.sql", "MANIFEST"]


def _sous_mon_compte(monkeypatch):
    """Le propriétaire attendu est `postgres` en production ; la suite de
    tests, elle, tourne sous un autre compte. On vérifie donc la RÈGLE, pas
    l'environnement de celui qui lance les tests."""
    import pwd

    from pgtool import engine

    monkeypatch.setattr(engine, "PROPRIETAIRE_ATTENDU",
                        pwd.getpwuid(os.getuid()).pw_name)


def test_un_mode_trop_ouvert_est_une_anomalie(store, monkeypatch):
    """Le bash affichait le mode sans jamais le commenter. Une garantie qu'on
    veut voir violée mérite d'être dite, pas seulement montrée."""
    _sous_mon_compte(monkeypatch)
    d = _snap(store.dest, "20260820-093240",
              fichiers=("forgejo.dump", "globals.sql"))
    (d / "forgejo.dump").chmod(0o644)
    (d / "globals.sql").chmod(0o600)
    anomalies = show_anomalies(store.resolve("20260820-093240"))
    assert any("forgejo.dump" in a and "644" in a for a in anomalies)
    assert not any("globals.sql" in a for a in anomalies)


def test_un_proprietaire_inattendu_est_une_anomalie(store, monkeypatch):
    """Un dump qui n'appartient pas à postgres est illisible par le moteur au
    moment où l'on en a besoin."""
    from pgtool import engine

    monkeypatch.setattr(engine, "PROPRIETAIRE_ATTENDU", "un-autre-compte")
    d = _snap(store.dest, "20260820-093240", fichiers=("forgejo.dump",))
    (d / "forgejo.dump").chmod(0o600)
    anomalies = show_anomalies(store.resolve("20260820-093240"))
    assert any("appartient" in a for a in anomalies)


def test_globals_absent_est_une_anomalie(store, monkeypatch):
    """Sans les rôles, une restauration refusera : mieux vaut l'apprendre en
    inspectant l'instantané qu'au moment de s'en servir."""
    _sous_mon_compte(monkeypatch)
    d = _snap(store.dest, "20260820-093240", fichiers=("forgejo.dump",))
    (d / "forgejo.dump").chmod(0o600)
    anomalies = show_anomalies(store.resolve("20260820-093240"))
    assert any("globals.sql absent" in a for a in anomalies)


def test_un_instantane_bien_range_na_pas_danomalie(store, monkeypatch):
    _sous_mon_compte(monkeypatch)
    d = _snap(store.dest, "20260820-093240",
              fichiers=("forgejo.dump", "globals.sql", "MANIFEST"))
    for f in d.iterdir():
        f.chmod(0o600)
    assert show_anomalies(store.resolve("20260820-093240")) == []
