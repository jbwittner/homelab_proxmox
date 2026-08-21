"""La sauvegarde locale : l'élagage, l'atomicité, le manifeste.

Deux propriétés se défendent ici, et ce sont celles dont la violation ne
produit aucun message :

  - **il reste toujours au moins un instantané.** Une source de vérité sans
    aucune sauvegarde est le seul état dont on ne se relève pas ;
  - **un répertoire présent est une sauvegarde COMPLÈTE.** Les `.part` sont
    des exécutions interrompues ; les compter, c'est croire à un filet qui
    n'existe pas — et pire, en copier un hors-site déposerait dans le bucket
    un objet tronqué que le compte de service ne peut pas remplacer.
"""

from __future__ import annotations

import pytest

from fjtool import backup as B


def _instantane(racine, nom, *, partiel=False):
    chemin = racine / (f"{nom}.part" if partiel else nom)
    chemin.mkdir(parents=True)
    (chemin / "forgejo.dump").write_bytes(b"x" * 10)
    return chemin


# ─── ce qui compte comme un instantané ───────────────────────────────────────


def test_les_executions_interrompues_ne_comptent_pas(tmp_path):
    _instantane(tmp_path, "20260821-020000")
    _instantane(tmp_path, "20260821-030000", partiel=True)
    noms = [c.name for c in B.instantanes(tmp_path)]
    assert noms == ["20260821-020000"]


def test_ce_qui_ne_ressemble_pas_a_une_date_est_ignore(tmp_path):
    """`latest` est un lien, `lost+found` un répertoire du système : ni l'un
    ni l'autre n'est une sauvegarde."""
    _instantane(tmp_path, "20260821-020000")
    (tmp_path / "lost+found").mkdir()
    (tmp_path / "latest").symlink_to("20260821-020000")
    assert [c.name for c in B.instantanes(tmp_path)] == ["20260821-020000"]


def test_les_instantanes_sortent_du_plus_ancien_au_plus_recent(tmp_path):
    """L'ordre porte l'élagage : c'est en tête qu'on coupe."""
    for nom in ("20260821-030000", "20260819-020000", "20260820-020000"):
        _instantane(tmp_path, nom)
    noms = [c.name for c in B.instantanes(tmp_path)]
    assert noms == sorted(noms)
    assert noms[0] == "20260819-020000"


def test_un_repertoire_absent_ne_leve_pas(tmp_path):
    """Le volume peut ne pas être monté : c'est un cas à diagnostiquer, pas à
    faire remonter en trace de pile."""
    assert B.instantanes(tmp_path / "jamais") == []


# ─── l'élagage ───────────────────────────────────────────────────────────────


def test_l_elagage_respecte_la_retention(tmp_path):
    existants = [_instantane(tmp_path, f"2026082{i}-020000") for i in range(5)]
    vises = B.a_elaguer(existants, retention=3)
    assert [c.name for c in vises] == [existants[0].name, existants[1].name]


def test_rien_ne_sefface_sous_la_retention(tmp_path):
    existants = [_instantane(tmp_path, f"2026082{i}-020000") for i in range(3)]
    assert B.a_elaguer(existants, retention=14) == []


def test_le_dernier_instantane_nest_jamais_efface(tmp_path):
    """La garde qui compte. Une rétention réglée à 0 par erreur, ou une
    horloge qui saute, effacerait tout ce qui reste."""
    existants = [_instantane(tmp_path, f"2026082{i}-020000") for i in range(3)]
    for retention in (0, -1, 1):
        vises = B.a_elaguer(existants, retention=retention)
        assert existants[-1] not in vises, f"le dernier visé avec retention={retention}"


def test_un_seul_instantane_nest_jamais_elague(tmp_path):
    seul = [_instantane(tmp_path, "20260821-020000")]
    assert B.a_elaguer(seul, retention=0) == []


# ─── le paramétrage ──────────────────────────────────────────────────────────


def test_les_valeurs_viennent_de_l_environnement():
    cfg = B.Config.from_env({
        "FJ_BACKUP_DEST": "/tmp/pra",
        "FJ_BACKUP_RETENTION": "7",
        "FJ_BACKUP_MIN_FREE_MB": "1024",
    })
    assert str(cfg.dest) == "/tmp/pra"
    assert cfg.retention == 7
    assert cfg.min_free_mb == 1024


def test_une_variable_absente_reprend_le_defaut():
    cfg = B.Config.from_env({})
    assert cfg.retention == 14
    assert str(cfg.dest) == "/var/backups/forgejo"


def test_une_valeur_illisible_est_un_refus_et_non_un_repli():
    """Un `FJ_BACKUP_RETENTION=quatorze` qui retomberait sur 14 donnerait
    l'illusion d'avoir été lu — et la faute de frappe survivrait des mois."""
    with pytest.raises(SystemExit):
        B.Config.from_env({"FJ_BACKUP_RETENTION": "quatorze"})


# ─── le manifeste ────────────────────────────────────────────────────────────


def test_le_manifeste_se_relit_lui_meme():
    texte = B.rendre_manifeste(
        stamp="20260821-024500",
        version="v15.0.3",
        etat=B.EtatDepots(depots=12, octets=987654, dernier_mtime=1755000000),
        taille_dump=4096,
    )
    valeurs = B.lire_manifeste_texte(texte)
    assert valeurs["STAMP"] == "20260821-024500"
    assert valeurs["FORGEJO_VERSION"] == "v15.0.3"
    assert valeurs["REPOS_COUNT"] == "12"
    assert valeurs["DUMP_BYTES"] == "4096"


def test_le_manifeste_dit_que_les_depots_ny_sont_pas():
    """Le manifeste se lit un mauvais jour, par quelqu'un qui ne se souvient
    pas du découpage. Il doit porter lui-même l'avertissement, sinon on
    restaure une base en croyant avoir tout."""
    texte = B.rendre_manifeste(
        stamp="20260821-024500", version="v15.0.3",
        etat=B.EtatDepots(0, 0, 0), taille_dump=0,
    )
    assert "LES DÉPÔTS NE SONT PAS DANS CETTE SAUVEGARDE" in texte
    assert "vzdump" in texte


def test_le_manifeste_porte_de_quoi_apparier_un_vzdump():
    """Les trois nombres qui permettent de dire « ce vzdump n'est pas du même
    moment que ce dump » — la seule question qui compte en reprise."""
    texte = B.rendre_manifeste(
        stamp="s", version="v", etat=B.EtatDepots(3, 42, 99), taille_dump=1,
    )
    valeurs = B.lire_manifeste_texte(texte)
    assert valeurs["REPOS_COUNT"] == "3"
    assert valeurs["REPOS_BYTES"] == "42"
    assert valeurs["REPOS_LAST_MTIME"] == "99"


# ─── le relevé de l'arborescence ─────────────────────────────────────────────


def test_le_releve_compte_les_depots_et_leurs_octets(tmp_path):
    depot = tmp_path / "org" / "projet.git"
    depot.mkdir(parents=True)
    (depot / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    etat = B.EtatDepots.relever(tmp_path)
    assert etat.depots == 1
    assert etat.octets > 0
    assert etat.dernier_mtime > 0


def test_le_releve_dune_racine_absente_rend_des_zeros(tmp_path):
    """Une instance neuve n'a aucun dépôt : ce n'est pas une panne."""
    etat = B.EtatDepots.relever(tmp_path / "jamais")
    assert etat == B.EtatDepots(0, 0, 0)


# ─── l'horodatage ────────────────────────────────────────────────────────────


def test_l_horodatage_est_triable_lexicographiquement():
    """C'est ce qui permet à `sorted()` de rendre l'ordre chronologique, et
    donc à l'élagage de couper en tête sans analyser de date."""
    from datetime import datetime

    tot = B.horodatage(datetime(2026, 8, 21, 2, 45, 0))
    tard = B.horodatage(datetime(2026, 8, 21, 3, 50, 0))
    assert tot == "20260821-024500"
    assert tot < tard
