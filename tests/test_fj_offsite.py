"""La copie hors-site : les refus, et la hiérarchie des verdicts.

Le compte de service a des droits **volontairement incomplets** — il liste,
lit et crée, il n'écrase ni ne supprime. Toute la conception en découle, et
ces tests en défendent les conséquences :

  - un objet distant qui diverge ne se répare pas d'ici, il se SIGNALE ;
  - un `.part` ne part jamais : déposé dans le bucket, il y resterait pour
    toujours, puisque rien ne peut le remplacer ;
  - un listage distant en échec veut dire « on ne sait pas », jamais « rien à
    copier ».
"""

from __future__ import annotations

import pytest

from fjtool import offsite as O


# ─── ce qui part ─────────────────────────────────────────────────────────────


def test_une_execution_interrompue_ne_part_jamais(tmp_path):
    """Le cas qui coûte le plus cher : un objet tronqué déposé dans un bucket
    où le compte de service n'a pas le droit d'écraser y reste pour toujours,
    et la sauvegarde de ce jour-là est perdue sans remède."""
    (tmp_path / "20260821-020000").mkdir()
    (tmp_path / "20260821-030000.part").mkdir()
    assert [c.name for c in O.instantanes_locaux(tmp_path)] == ["20260821-020000"]


def test_une_source_absente_ne_leve_pas(tmp_path):
    """Le volume peut ne pas être monté : c'est un refus argumenté, pas une
    trace de pile dans le journal de 3h50."""
    assert O.instantanes_locaux(tmp_path / "jamais") == []


def test_les_fichiers_sont_releves_recursivement(tmp_path):
    snap = tmp_path / "20260821-020000"
    (snap / "sous").mkdir(parents=True)
    (snap / "forgejo.dump").write_bytes(b"x")
    (snap / "sous" / "app.ini").write_bytes(b"y")
    assert O.fichiers_relatifs(snap) == ["forgejo.dump", "sous/app.ini"]


# ─── l'arborescence distante ─────────────────────────────────────────────────


def test_le_prefixe_distant_nomme_le_noeud_et_le_service():
    """Un bucket partagé entre deux nœuds et deux services n'est lisible que
    si le chemin dit d'où vient chaque objet — sinon une reprise commence par
    deviner."""
    cfg = O.OffsiteConfig(node="pve-eranikus", src=None, subpath="forgejo")
    assert cfg.prefix == "pve-eranikus/forgejo"


def test_le_noeud_vient_de_l_unite_pas_du_hostname():
    """Le drop-in du nœud fait autorité : c'est lui qui rend la copie juste
    sur une machine renommée."""
    cfg = O.OffsiteConfig.from_env(
        {"FJBK_OFFSITE_NODE": "pve-eranikus"}, hostname="autre"
    )
    assert cfg.node == "pve-eranikus"


def test_le_hostname_sert_de_repli():
    cfg = O.OffsiteConfig.from_env({}, hostname="pve-eranikus")
    assert cfg.node == "pve-eranikus"


def test_une_valeur_entiere_illisible_est_un_refus():
    """Comme pour la sauvegarde : un repli silencieux ferait survivre la
    faute de frappe des mois durant."""
    with pytest.raises(O.Preflight):
        O.OffsiteConfig.from_env(
            {"FJBK_OFFSITE_TRANSFERS": "quatre"}, hostname="n"
        )


# ─── les refus préalables ────────────────────────────────────────────────────


def _cfg(tmp_path, **surcharges):
    defauts = dict(
        node="pve-eranikus",
        src=tmp_path / "src",
        config=tmp_path / "rclone.conf",
        key=tmp_path / "cle.json",
        binary=str(tmp_path / "rclone"),
    )
    defauts.update(surcharges)
    return O.OffsiteConfig(**defauts)


def _tout_poser(tmp_path):
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "rclone.conf").write_text("[gcs]\n")
    (tmp_path / "cle.json").write_text("{}")
    (tmp_path / "rclone").write_text("#!/bin/sh\n")


def test_lancer_hors_root_est_refuse(tmp_path):
    """Les dumps appartiennent à un UID de conteneur non privilégié : aucun
    autre compte du nœud ne peut les lire, et l'échec ressemblerait à un
    répertoire vide."""
    _tout_poser(tmp_path)
    with pytest.raises(O.Preflight) as capture:
        O.preflight(_cfg(tmp_path), euid=1000)
    assert "root" in str(capture.value)


def test_une_cle_vide_est_refusee(tmp_path):
    """Un fichier de zéro octet passe `is_file()` : c'est exactement ce qu'un
    `touch` de dépannage laisse derrière lui."""
    _tout_poser(tmp_path)
    (tmp_path / "cle.json").write_text("")
    with pytest.raises(O.Preflight) as capture:
        O.preflight(_cfg(tmp_path), euid=0)
    assert "OpenBao" in str(capture.value), "le refus doit dire où reprendre la clé"


def test_une_source_qui_nest_pas_un_repertoire_est_refusee(tmp_path):
    """La confusion la plus facile de tout ce montage : la VUE CONTENEUR
    (`/var/backups/forgejo`) au lieu de la VUE HÔTE."""
    _tout_poser(tmp_path)
    with pytest.raises(O.Preflight) as capture:
        O.preflight(_cfg(tmp_path, src=tmp_path / "inexistant"), euid=0)
    assert "VUE HÔTE" in str(capture.value)


def test_un_environnement_complet_passe(tmp_path):
    _tout_poser(tmp_path)
    O.preflight(_cfg(tmp_path), euid=0)  # ne lève pas


# ─── la hiérarchie des verdicts ──────────────────────────────────────────────


def test_tout_en_ligne_donne_zero():
    assert O.verdict([O.Sort.EN_LIGNE, O.Sort.TRANSFERE]) == O.EXIT_OK


def test_un_echec_donne_deux():
    assert O.verdict([O.Sort.EN_LIGNE, O.Sort.ECHEC]) == O.EXIT_FAILED


def test_la_divergence_l_emporte_sur_l_echec():
    """Un transfert raté sera retenté demain tout seul ; un objet divergent ne
    se réparera JAMAIS de lui-même, puisque le compte de service ne peut pas
    écraser. Le code le plus élevé doit désigner ce qui demande quelqu'un."""
    assert O.verdict([O.Sort.ECHEC, O.Sort.DIVERGENT]) == O.EXIT_DIVERGENT


def test_aucun_instantane_est_un_probleme_d_environnement():
    """Et non un succès : « rien à copier » et « tout est copié » ne doivent
    pas rendre le même code."""
    assert O.verdict([]) == O.EXIT_ENV


def test_les_codes_sont_distincts():
    """Chaque famille de panne a son code — c'est ce qui permet de lire un
    journal systemd de trois semaines sans rejouer la commande."""
    codes = {O.EXIT_OK, O.EXIT_ENV, O.EXIT_FAILED, O.EXIT_DIVERGENT, O.EXIT_SIGNAL}
    assert len(codes) == 5


# ─── l'âge ───────────────────────────────────────────────────────────────────


def test_l_age_se_compte_en_heures(tmp_path):
    import os

    snap = tmp_path / "20260821-020000"
    snap.mkdir()
    maintenant = 1_755_000_000
    os.utime(snap, (maintenant - 7200, maintenant - 7200))
    assert O.age_heures(snap, maintenant) == 2
