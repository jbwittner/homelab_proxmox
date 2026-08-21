"""Le modèle d'instantané : résolution, complétude, rétention.

Cette logique existait en TROIS exemplaires dans le bash, avec des sémantiques
divergentes — l'âge en jours calendaires dans `pgbk list`, `find -mtime` par
périodes de 24 h dans `prune`. Une seule implémentation ici, et des tests qui
figent celle du bash, parce que c'est elle qui décide ce qui est purgé.
"""

from __future__ import annotations

import os

import pytest

from pgtool.snapshots import (
    JOUR,
    Reference,
    Snapshot,
    Store,
    age_jours,
    expires,
    parse_reference,
)


# ─── mise en place ───────────────────────────────────────────────────────────


def _snap(racine, nom, *, fichiers=("globals.sql", "forge.dump", "MANIFEST"),
          age_h=0, maintenant=1_800_000_000):
    d = racine / nom
    d.mkdir(parents=True)
    for f in fichiers:
        (d / f).write_text(f"contenu de {f}\n")
    quand = maintenant - age_h * 3600
    os.utime(d, (quand, quand))
    return d


@pytest.fixture
def dest(tmp_path):
    d = tmp_path / "postgresql"
    d.mkdir()
    return d


# ─── analyse d'une référence ─────────────────────────────────────────────────


def test_latest_est_une_reference():
    assert parse_reference("latest") is Reference.LATEST


def test_un_horodatage_complet_est_une_reference():
    assert parse_reference("20260820-093240") is Reference.STAMP


def test_une_date_seule_est_une_reference():
    assert parse_reference("20260820") is Reference.DAY


def test_une_reference_incomprise_est_refusee():
    """Le bash disait « attendu: latest, AAAAMMJJ, ou AAAAMMJJ-HHMMSS ». Une
    référence fantaisiste ne doit pas se retrouver interprétée comme un chemin."""
    for ref in ("hier", "", "../autre", "20260820-093240/../..", "latest2"):
        assert parse_reference(ref) is Reference.INVALID, ref


def test_un_part_nest_pas_une_reference_valide():
    """Le bash acceptait « 20260820-093240.part » sur la branche horodatage :
    la référence passait, et seul `delete` le rattrapait plus loin. Ici c'est
    refusé dès l'analyse — un instantané incomplet n'est pas restaurable."""
    assert parse_reference("20260820-093240.part") is Reference.INVALID


# ─── inventaire ──────────────────────────────────────────────────────────────


def test_les_instantanes_sont_tries_chronologiquement(dest):
    for nom in ("20260820-093240", "20260819-233627", "20260820-020000"):
        _snap(dest, nom)
    assert [s.name for s in Store(dest).snapshots()] == [
        "20260819-233627", "20260820-020000", "20260820-093240",
    ]


def test_une_execution_en_cours_nest_pas_un_instantane(dest):
    """Tout est écrit dans <stamp>.part puis renommé : un répertoire SANS ce
    suffixe est complet par construction."""
    _snap(dest, "20260820-093240")
    _snap(dest, "20260820-100000.part")
    assert [s.name for s in Store(dest).snapshots()] == ["20260820-093240"]


def test_un_filet_de_restauration_nest_pas_un_instantane(dest):
    _snap(dest, "20260820-093240")
    _snap(dest, "pre-restore-20260820-101500")
    assert [s.name for s in Store(dest).snapshots()] == ["20260820-093240"]


def test_un_instantane_connait_ses_bases(dest):
    _snap(dest, "20260820-093240", fichiers=("globals.sql", "forge.dump",
                                             "wiki.dump", "MANIFEST"))
    s = Store(dest).snapshots()[0]
    assert s.databases == ("forge", "wiki")


def test_un_instantane_sait_sil_a_son_manifeste(dest):
    _snap(dest, "20260820-093240", fichiers=("forge.dump",))
    assert Store(dest).snapshots()[0].has_manifest is False


def test_un_instantane_sait_sil_a_ses_globals(dest):
    """Sans globals.sql, les rôles manquent : `restore` refusera, et c'est ce
    refus qui rappelle qu'on les repose d'abord."""
    _snap(dest, "20260820-093240", fichiers=("forge.dump", "MANIFEST"))
    assert Store(dest).snapshots()[0].has_globals is False


# ─── résolution ──────────────────────────────────────────────────────────────


def test_latest_suit_le_lien(dest):
    cible = _snap(dest, "20260820-093240")
    (dest / "latest").symlink_to(cible)
    assert Store(dest).resolve("latest").path == cible


def test_latest_absent_le_dit(dest):
    _snap(dest, "20260820-093240")
    with pytest.raises(LookupError, match="latest"):
        Store(dest).resolve("latest")


def test_latest_pendant_le_dit(dest):
    """Le lien est absolu et pointe dans le CT : vu d'ailleurs il est cassé."""
    (dest / "latest").symlink_to(dest / "disparu")
    with pytest.raises(LookupError):
        Store(dest).resolve("latest")


def test_un_horodatage_designe_son_repertoire(dest):
    cible = _snap(dest, "20260820-093240")
    assert Store(dest).resolve("20260820-093240").path == cible


def test_une_date_seule_prend_la_plus_recente_du_jour(dest):
    """C'est le piège de `delete` : « 20260819 » peut désigner le dernier
    instantané, celui qui est protégé."""
    _snap(dest, "20260819-023318")
    tard = _snap(dest, "20260819-234458")
    assert Store(dest).resolve("20260819").path == tard


def test_une_date_seule_ignore_les_executions_en_cours(dest):
    tard = _snap(dest, "20260819-234458")
    _snap(dest, "20260819-235959.part")
    assert Store(dest).resolve("20260819").path == tard


def test_une_date_sans_instantane_le_dit(dest):
    _snap(dest, "20260820-093240")
    with pytest.raises(LookupError, match="20260101"):
        Store(dest).resolve("20260101")


def test_un_horodatage_inexistant_le_dit(dest):
    with pytest.raises(LookupError, match="introuvable"):
        Store(dest).resolve("20260820-093240")


def test_une_reference_incomprise_ne_touche_pas_au_disque(dest):
    with pytest.raises(ValueError, match="attendu"):
        Store(dest).resolve("../../etc")


def test_la_resolution_reste_sous_la_destination(dest, tmp_path):
    """Garde de confinement : le bash la posait après coup, avec un commentaire
    sur le risque d'un `rm -rf $PWD`. Ici rien ne sort du répertoire."""
    dehors = tmp_path / "ailleurs"
    dehors.mkdir()
    (dest / "latest").symlink_to(dehors)
    with pytest.raises(LookupError):
        Store(dest).resolve("latest")


# ─── âge et rétention : la sémantique de find -mtime ─────────────────────────


def test_lage_est_en_periodes_de_24h_tronquees():
    """`find -mtime` compte des périodes de 24 h et TRONQUE. Ce n'est pas un
    nombre de jours calendaires."""
    assert age_jours(mtime=0, maintenant=JOUR - 1) == 0
    assert age_jours(mtime=0, maintenant=JOUR) == 1
    assert age_jours(mtime=0, maintenant=2 * JOUR - 1) == 1


def test_expire_a_partir_de_n_plus_un_jours():
    """`-mtime +14` veut dire « âge tronqué STRICTEMENT supérieur à 14 », donc
    une suppression à partir de 15 × 24 h — pas de 14 jours calendaires. Une
    implémentation « âge > 14 jours » supprimerait un jour trop tôt."""
    assert expires(mtime=0, maintenant=14 * JOUR, retention=14) is False
    assert expires(mtime=0, maintenant=15 * JOUR - 1, retention=14) is False
    assert expires(mtime=0, maintenant=15 * JOUR, retention=14) is True


def test_le_changement_dheure_ne_decale_rien():
    """Le passage à l'heure d'hiver ajoute une heure au calendrier local, pas à
    l'horloge epoch. Une implémentation en jours calendaires purgerait un
    instantané avec un décalage d'un cran ce jour-là ; celle-ci ne bouge pas.

    Repère : 26 octobre 2025 03:00 CEST → 02:00 CET.
    """
    bascule = 1_761_440_400  # 2025-10-26 01:00 UTC, l'instant du recul
    veille = bascule - 13 * JOUR
    # 14 périodes de 24 h plus tard, à la seconde près, malgré l'heure en plus.
    assert expires(mtime=veille, maintenant=veille + 15 * JOUR - 1,
                   retention=14) is False
    assert expires(mtime=veille, maintenant=veille + 15 * JOUR,
                   retention=14) is True


def test_les_debris_expirent_a_48h():
    """`find -name '*.part' -mtime +1` : les débris de moins de 48 h sont
    laissés, parce qu'ils peuvent être une sauvegarde en cours."""
    assert expires(mtime=0, maintenant=2 * JOUR - 1, retention=1) is False
    assert expires(mtime=0, maintenant=2 * JOUR, retention=1) is True


def test_lage_porte_sur_le_mtime_pas_sur_le_nom(dest):
    """Le renommage <stamp>.part → <stamp> conserve le mtime, qui vaut donc
    l'instant d'achèvement. Lire le nom donnerait un autre résultat sur un
    instantané recopié ou touché après coup."""
    d = _snap(dest, "20260101-000000", age_h=1, maintenant=1_800_000_000)
    s = Store(dest).snapshots()[0]
    assert s.age_days(maintenant=1_800_000_000) == 0, "récent malgré son nom"
    assert d.name.startswith("20260101")


def test_ce_qui_serait_purge_se_calcule_sans_rien_supprimer(dest):
    """Le modèle Python PRÉDIT ce que le bash purgera ; c'est pg-backup.sh, en
    bash, qui purge réellement."""
    maintenant = 1_800_000_000
    _snap(dest, "20260101-000000", age_h=20 * 24, maintenant=maintenant)
    _snap(dest, "20260110-000000", age_h=10 * 24, maintenant=maintenant)
    _snap(dest, "20260120-000000.part", age_h=60, maintenant=maintenant)
    _snap(dest, "20260121-000000.part", age_h=10, maintenant=maintenant)

    store = Store(dest)
    expires_noms = [s.name for s in store.expired(retention=14,
                                                  maintenant=maintenant)]
    debris_noms = [s.name for s in store.debris(maintenant=maintenant)]

    assert expires_noms == ["20260101-000000"]
    assert debris_noms == ["20260120-000000.part"], "celui de 10 h est peut-être en cours"
    assert (dest / "20260101-000000").is_dir(), "rien n'a été supprimé"


def test_le_dernier_instantane_est_identifie(dest):
    """Protection de `delete` : c'est le plus récent par le nom, et le lien
    latest sert de repli s'il existe."""
    _snap(dest, "20260819-233627")
    dernier = _snap(dest, "20260820-093240")
    assert Store(dest).latest().path == dernier


def test_sans_aucun_instantane_il_ny_a_pas_de_dernier(dest):
    assert Store(dest).latest() is None
