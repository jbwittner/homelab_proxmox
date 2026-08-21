"""`pg offsite` — le contrat de codes de retour, et la décision de transfert.

Aucun bucket, aucun rclone, aucun réseau : l'inventaire local se teste sur un
`tmp_path`, le distant sur un `FakeRunner`. Le diff, lui, est une fonction pure
de deux listes et ne demande ni l'un ni l'autre.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from core.runner import CommandError, FakeRunner, Result
from pgtool.offsite import (
    EXIT_DIVERGENT,
    EXIT_ENV,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_SIGNAL,
    OffsiteConfig,
    Preflight,
    Snap,
    SnapshotDiff,
    age_hours,
    local_snapshots,
    preflight,
    push_snapshot,
    relative_files,
    run,
    verdict,
)
from core.commands import Rclone


# ─── inventaire local ────────────────────────────────────────────────────────


def _instantane(racine, nom, fichiers=("globals.sql", "base.dump", "MANIFEST")):
    d = racine / nom
    d.mkdir()
    for f in fichiers:
        (d / f).write_text(f"contenu de {f}")
    return d


def test_les_instantanes_sont_dans_lordre_chronologique(tmp_path):
    """L'horodatage AAAAMMJJ-HHMMSS fait coïncider ordre lexicographique et
    ordre chronologique : trier les noms suffit."""
    for nom in ("20260820-093240", "20260819-233627", "20260820-020000"):
        _instantane(tmp_path, nom)
    noms = [d.name for d in local_snapshots(tmp_path)]
    assert noms == ["20260819-233627", "20260820-020000", "20260820-093240"]


def test_une_execution_en_cours_nest_jamais_transferee(tmp_path):
    """Par construction de pg-backup.sh, un répertoire SANS le suffixe .part
    est complet — et un répertoire avec ne l'est pas."""
    _instantane(tmp_path, "20260820-093240")
    _instantane(tmp_path, "20260820-100000.part")
    assert [d.name for d in local_snapshots(tmp_path)] == ["20260820-093240"]


def test_un_filet_de_restauration_nest_jamais_transfere(tmp_path):
    """pre-restore-* : local, temporaire, sans valeur distante."""
    _instantane(tmp_path, "20260820-093240")
    _instantane(tmp_path, "pre-restore-20260820-101500")
    assert [d.name for d in local_snapshots(tmp_path)] == ["20260820-093240"]


def test_le_symlink_latest_nest_jamais_transfere(tmp_path):
    """Il pointe en absolu vers un chemin qui n'existe que dans le CT : vu de
    l'hôte il est cassé."""
    cible = _instantane(tmp_path, "20260820-093240")
    (tmp_path / "latest").symlink_to("/var/backups/postgresql/20260820-093240")
    assert [d.name for d in local_snapshots(tmp_path)] == [cible.name]


def test_un_latest_valide_est_quand_meme_ecarte(tmp_path):
    """Même résolvable, il ferait un doublon distant du même instantané."""
    _instantane(tmp_path, "20260820-093240")
    (tmp_path / "latest").symlink_to(tmp_path / "20260820-093240")
    assert [d.name for d in local_snapshots(tmp_path)] == ["20260820-093240"]


def test_les_fichiers_isoles_sont_ignores(tmp_path):
    _instantane(tmp_path, "20260820-093240")
    (tmp_path / "20260820-note.txt").write_text("x")
    assert len(local_snapshots(tmp_path)) == 1


def test_relative_files_descend_dans_les_sous_repertoires(tmp_path):
    d = _instantane(tmp_path, "20260820-093240", fichiers=("a.dump",))
    (d / "sous").mkdir()
    (d / "sous" / "b.dump").write_text("x")
    assert relative_files(d) == ["a.dump", "sous/b.dump"]


def test_age_en_heures_pleines(tmp_path):
    d = _instantane(tmp_path, "20260820-093240")
    os.utime(d, (0, 1_000_000))
    assert age_hours(d, 1_000_000 + 3600 * 50 + 59) == 50


# ─── le diff, fonction pure ──────────────────────────────────────────────────


def test_diff_ne_retient_que_ce_qui_manque():
    d = SnapshotDiff("s", ("a", "b", "c"), ("a", "c"))
    assert d.missing == ("b",)


def test_diff_trie_les_manquants():
    d = SnapshotDiff("s", ("z", "a"), ())
    assert d.missing == ("a", "z")


def test_un_objet_distant_en_trop_ne_nous_regarde_pas():
    """`check --one-way` : le bucket peut porter davantage, ce n'est pas une
    divergence de notre côté."""
    d = SnapshotDiff("s", ("a",), ("a", "vieux"))
    assert d.missing == ()


def test_diff_repere_labsence_de_manifeste():
    assert SnapshotDiff("s", ("a.dump",), ()).has_manifest is False
    assert SnapshotDiff("s", ("a.dump", "MANIFEST"), ()).has_manifest is True


def test_diff_repere_un_repertoire_vide():
    assert SnapshotDiff("s", (), ()).empty is True


# ─── contrôles préalables ────────────────────────────────────────────────────


@pytest.fixture
def cfg_valide(tmp_path):
    src = tmp_path / "data"
    src.mkdir()
    cle = tmp_path / "cle.json"
    cle.write_text(json.dumps({"private_key": "-----BEGIN-----"}))
    rclone = tmp_path / "rclone"
    rclone.write_text("#!/bin/sh\n")
    rclone.chmod(0o755)
    conf = tmp_path / "rclone.conf"
    conf.write_text("[gcs]\n")
    return OffsiteConfig(
        node="un-noeud", src=src, key=cle, config=conf, rclone=str(rclone),
        bucket="un-bucket",
    )


def test_preflight_passe_sur_un_environnement_complet(cfg_valide):
    preflight(cfg_valide, euid=0)


def test_preflight_exige_root(cfg_valide):
    """Les dumps sont en 600, propriété d'un UID de CT non privilégié."""
    with pytest.raises(Preflight, match="root"):
        preflight(cfg_valide, euid=1000)


def test_preflight_refuse_une_cle_tronquee(cfg_valide, tmp_path):
    """Une clé collée de travers se voit ici, pas sur un 401 incompréhensible."""
    cfg_valide.key.write_text('{"client_email": "x@y"}')
    with pytest.raises(Preflight, match="private_key"):
        preflight(cfg_valide, euid=0)


def test_preflight_refuse_une_cle_vide(cfg_valide):
    cfg_valide.key.write_text("")
    with pytest.raises(Preflight, match="absente ou vide"):
        preflight(cfg_valide, euid=0)


def test_preflight_refuse_une_source_absente(cfg_valide):
    cfg_valide.src.rmdir()
    with pytest.raises(Preflight, match="VUE HÔTE"):
        preflight(cfg_valide, euid=0)


def test_preflight_refuse_un_rclone_absent(cfg_valide):
    cfg = OffsiteConfig(
        node="n", src=cfg_valide.src, key=cfg_valide.key,
        config=cfg_valide.config, rclone="/nexiste/pas",
    )
    with pytest.raises(Preflight, match="rclone introuvable"):
        preflight(cfg, euid=0)


# ─── verdict : le contrat de codes de retour ─────────────────────────────────


def test_tout_en_ligne_rend_zero():
    assert verdict([Snap.ONLINE, Snap.ONLINE]) == EXIT_OK


def test_un_transfert_rend_zero():
    """Transférer n'est pas une anomalie : c'est le travail."""
    assert verdict([Snap.TRANSFERRED, Snap.ONLINE]) == EXIT_OK


def test_un_echec_rend_deux():
    assert verdict([Snap.ONLINE, Snap.FAILED]) == EXIT_FAILED


def test_une_divergence_rend_trois():
    assert verdict([Snap.ONLINE, Snap.DIVERGENT]) == EXIT_DIVERGENT


def test_la_divergence_lemporte_sur_lechec():
    """Un transfert raté se rejoue tout seul à la prochaine exécution ; un
    objet distant divergent demande une intervention humaine et ne doit pas
    être masqué par un échec transitoire."""
    assert verdict([Snap.FAILED, Snap.DIVERGENT]) == EXIT_DIVERGENT


def test_le_dix_du_bash_nest_jamais_un_code_de_sortie():
    """Le bash faisait circuler 10 comme valeur de retour interne de
    push_snapshot. Ce n'était pas un code de processus, et il ne doit pas le
    devenir."""
    tous = {verdict([])} | {verdict([sort]) for sort in Snap}
    assert 10 not in tous
    assert tous <= {EXIT_OK, EXIT_FAILED, EXIT_DIVERGENT}


def test_les_codes_sont_ceux_de_lunite_systemd():
    """Les unités et les habitudes ne doivent pas changer."""
    assert (EXIT_OK, EXIT_ENV, EXIT_FAILED, EXIT_DIVERGENT, EXIT_SIGNAL) == (
        0, 1, 2, 3, 130,
    )


# ─── transfert d'un instantané ───────────────────────────────────────────────


def _rc(runner, cfg):
    return Rclone(runner, cfg.rclone_config())


def test_deja_en_ligne_ne_copie_rien(tmp_path, cfg_valide):
    d = _instantane(tmp_path, "20260820-093240")
    r = FakeRunner()
    r.when("lsf", Result(("rclone",), 0, "globals.sql\nbase.dump\nMANIFEST\n", ""))
    sort = push_snapshot(_rc(r, cfg_valide), cfg_valide, d, dry_run=False)
    assert sort is Snap.ONLINE
    assert not any("copy" in argv for argv in r.calls)


def test_objets_manquants_declenchent_une_copie(tmp_path, cfg_valide):
    d = _instantane(tmp_path, "20260820-093240")
    r = FakeRunner()
    r.when("lsf", Result(("rclone",), 0, "MANIFEST\n", ""))
    sort = push_snapshot(_rc(r, cfg_valide), cfg_valide, d, dry_run=False)
    assert sort is Snap.TRANSFERRED
    copie = [argv for argv in r.calls if "copy" in argv][0]
    assert "--ignore-existing" in copie
    assert "sync" not in copie


def test_un_listage_distant_impossible_est_un_echec_pas_une_anomalie(
    tmp_path, cfg_valide
):
    """Le nœud pourra réessayer : la prochaine exécution repart d'elle-même."""
    d = _instantane(tmp_path, "20260820-093240")
    r = FakeRunner()
    r.when("lsf", Result(("rclone",), 1, "", "connexion refusée"))
    assert push_snapshot(_rc(r, cfg_valide), cfg_valide, d, dry_run=False) is Snap.FAILED


def test_un_transfert_en_echec_narrete_pas_le_reste(tmp_path, cfg_valide):
    d = _instantane(tmp_path, "20260820-093240")
    r = FakeRunner()
    r.when("lsf", Result(("rclone",), 0, "", ""))
    r.when(lambda argv: "copy" in argv, Result(("rclone",), 1, "", "quota"))
    assert push_snapshot(_rc(r, cfg_valide), cfg_valide, d, dry_run=False) is Snap.FAILED


def test_un_distant_divergent_est_signale_jamais_repare(tmp_path, cfg_valide, capsys):
    """Le compte de service n'a pas objects.delete : une correction en boucle
    masquerait la seule anomalie que ce montage existe pour révéler."""
    d = _instantane(tmp_path, "20260820-093240")
    r = FakeRunner()
    r.when("lsf", Result(("rclone",), 0, "globals.sql\nbase.dump\nMANIFEST\n", ""))
    r.when(lambda argv: "check" in argv, Result(("rclone",), 1, "", "sizes differ"))
    sort = push_snapshot(_rc(r, cfg_valide), cfg_valide, d, dry_run=False)
    assert sort is Snap.DIVERGENT
    erreurs = capsys.readouterr().err
    assert "gcloud storage rm" in erreurs, "le message doit dire quoi faire"
    assert not any("delete" in argv or "sync" in argv for argv in r.calls)


def test_un_repertoire_vide_est_ignore_sans_echouer(tmp_path, cfg_valide):
    d = tmp_path / "20260820-093240"
    d.mkdir()
    r = FakeRunner()
    assert push_snapshot(_rc(r, cfg_valide), cfg_valide, d, dry_run=False) is Snap.ONLINE
    assert r.calls == []


def test_labsence_de_manifeste_avertit_mais_nempeche_pas(tmp_path, cfg_valide, capsys):
    """Des dumps sans manifeste valent mieux que rien."""
    d = _instantane(tmp_path, "20260820-093240", fichiers=("base.dump",))
    r = FakeRunner()
    r.when("lsf", Result(("rclone",), 0, "", ""))
    sort = push_snapshot(_rc(r, cfg_valide), cfg_valide, d, dry_run=False)
    assert sort is Snap.TRANSFERRED
    assert "pas de MANIFEST" in capsys.readouterr().err


# ─── simulation ──────────────────────────────────────────────────────────────


def test_la_simulation_necrit_rien(tmp_path, cfg_valide):
    d = _instantane(tmp_path, "20260820-093240")
    r = FakeRunner()
    r.when("lsf", Result(("rclone",), 0, "", ""))
    push_snapshot(_rc(r, cfg_valide), cfg_valide, d, dry_run=True)
    assert not any("copy" in argv for argv in r.calls)


def test_la_simulation_controle_un_instantane_complet(tmp_path, cfg_valide):
    """Le bash sortait avant le contrôle, ce qui rendait --dry-run aveugle au
    seul mode de panne autour duquel tout ce montage est conçu. Le contrôle est
    une lecture : rien n'interdit de le jouer."""
    d = _instantane(tmp_path, "20260820-093240")
    r = FakeRunner()
    r.when("lsf", Result(("rclone",), 0, "globals.sql\nbase.dump\nMANIFEST\n", ""))
    r.when(lambda argv: "check" in argv, Result(("rclone",), 1, "", "sizes differ"))
    sort = push_snapshot(_rc(r, cfg_valide), cfg_valide, d, dry_run=True)
    assert sort is Snap.DIVERGENT, "--dry-run doit pouvoir sortir en code 3"


def test_la_simulation_ne_conclut_pas_sur_un_instantane_incomplet(
    tmp_path, cfg_valide, capsys
):
    """Le contrôle porterait sur un instantané auquel il manque des objets et
    échouerait pour la mauvaise raison."""
    d = _instantane(tmp_path, "20260820-093240")
    r = FakeRunner()
    r.when("lsf", Result(("rclone",), 0, "MANIFEST\n", ""))
    sort = push_snapshot(_rc(r, cfg_valide), cfg_valide, d, dry_run=True)
    assert sort is Snap.TRANSFERRED
    assert "non évaluable" in capsys.readouterr().err
    assert not any("check" in argv for argv in r.calls)


# ─── déroulé complet ─────────────────────────────────────────────────────────


def _runner_sain():
    r = FakeRunner()
    r.when(lambda argv: "version" in argv, Result(("rclone",), 0, "rclone v1.60.1\n", ""))
    r.when(lambda argv: "--max-depth" in argv, Result(("rclone",), 0, "pve-eranikus/\n", ""))
    r.when(lambda argv: "lsf" in argv, Result(("rclone",), 0, "globals.sql\nbase.dump\nMANIFEST\n", ""))
    r.when(lambda argv: "size" in argv, Result(("rclone",), 0, "Total objects: 3\n", ""))
    return r


@pytest.mark.skipif(os.geteuid() != 0, reason="preflight exige root")
def test_run_complet_rend_zero(tmp_path, cfg_valide):  # pragma: no cover
    _instantane(cfg_valide.src, "20260820-093240")
    assert run(cfg_valide, _runner_sain(), dry_run=False, now=time.time()) == EXIT_OK


def test_run_sans_root_rend_un(cfg_valide, capsys):
    """Quel que soit le reste, un environnement inutilisable sort en 1."""
    code = run(cfg_valide, _runner_sain(), dry_run=False, now=time.time())
    if os.geteuid() == 0:  # pragma: no cover - dépend de l'environnement
        pytest.skip("lancé en root")
    assert code == EXIT_ENV
    assert "root" in capsys.readouterr().err


def test_aucune_sauvegarde_locale_rend_un(monkeypatch, cfg_valide, capsys):
    """Une source vide n'est pas un succès : on croirait avoir une copie."""
    monkeypatch.setattr("pgtool.offsite.preflight", lambda cfg, *, euid: None)
    code = run(cfg_valide, _runner_sain(), dry_run=False, now=time.time())
    assert code == EXIT_ENV
    assert "aucune sauvegarde locale" in capsys.readouterr().err


def test_bucket_injoignable_rend_un(monkeypatch, cfg_valide, capsys):
    monkeypatch.setattr("pgtool.offsite.preflight", lambda cfg, *, euid: None)
    _instantane(cfg_valide.src, "20260820-093240")
    r = _runner_sain()
    r.matchers.insert(
        0, (lambda argv: "--max-depth" in argv, Result(("rclone",), 1, "", "403"))
    )
    assert run(cfg_valide, r, dry_run=False, now=time.time()) == EXIT_ENV
    assert "bucket injoignable" in capsys.readouterr().err


def test_un_instantane_trop_vieux_avertit_sans_echouer(
    monkeypatch, cfg_valide, capsys
):
    """Le hors-site n'est pas responsable de la sauvegarde locale — mais une
    source qui ne bouge plus produirait des exécutions vertes qui ne protègent
    plus rien."""
    monkeypatch.setattr("pgtool.offsite.preflight", lambda cfg, *, euid: None)
    d = _instantane(cfg_valide.src, "20260820-093240")
    os.utime(d, (0, 0))
    code = run(cfg_valide, _runner_sain(), dry_run=False, now=time.time())
    assert code == EXIT_OK, "vieux n'est pas une erreur"
    assert "ne tourne peut-être plus" in capsys.readouterr().err


def test_le_bilan_dit_ce_qui_partirait_en_simulation(
    monkeypatch, cfg_valide, capsys
):
    monkeypatch.setattr("pgtool.offsite.preflight", lambda cfg, *, euid: None)
    _instantane(cfg_valide.src, "20260820-093240")
    r = _runner_sain()
    r.matchers.insert(0, (lambda argv: "lsf" in argv and "-R" in argv,
                          Result(("rclone",), 0, "", "")))
    run(cfg_valide, r, dry_run=True, now=time.time())
    assert "à transférer" in capsys.readouterr().out


# ─── paramétrage ─────────────────────────────────────────────────────────────


def test_les_defauts_viennent_de_lenvironnement():
    cfg = OffsiteConfig.from_env({}, hostname="un-noeud")
    assert cfg.node == "un-noeud"
    assert cfg.subpath == "postgresql"
    assert cfg.check_mode == "hash"


def test_lunite_systemd_prime_sur_les_defauts():
    cfg = OffsiteConfig.from_env(
        {
            "PGBK_OFFSITE_NODE": "pve-autre",
            "PGBK_OFFSITE_BUCKET": "autre-bucket",
            "PGBK_OFFSITE_TRANSFERS": "8",
            "PGBK_OFFSITE_STALE_HOURS": "24",
        },
        hostname="ignore",
    )
    assert cfg.node == "pve-autre"
    assert cfg.transfers == 8
    assert cfg.stale_hours == 24


def test_le_noeud_est_au_premier_niveau_distant():
    """Pour qu'un second nœud s'ajoute sans restructurer le bucket."""
    cfg = OffsiteConfig.from_env(
        {"PGBK_OFFSITE_NODE": "pve-eranikus", "PGBK_OFFSITE_BUCKET": "b"},
        hostname="x",
    )
    assert cfg.prefix == "pve-eranikus/postgresql"
    assert cfg.base == "gcs:b/pve-eranikus/postgresql"


def test_bwlimit_vide_veut_dire_pas_de_bridage():
    assert OffsiteConfig.from_env({}, hostname="x").bwlimit == ""
    cfg = OffsiteConfig.from_env({"PGBK_OFFSITE_BWLIMIT": "10M"}, hostname="x")
    assert cfg.bwlimit == "10M"


# ─── parité de la ligne de verdict ───────────────────────────────────────────


def test_le_verdict_porte_la_duree(monkeypatch, cfg_valide, capsys):
    """Le bash disait « terminé en 2s ». Une copie qui passe de deux secondes à
    quarante minutes est un signal ; sans la durée il faudrait soustraire des
    horodatages à la main dans journalctl.

    Écart constaté à la comparaison de parité du 21 août 2026.
    """
    import re

    monkeypatch.setattr("pgtool.offsite.preflight", lambda cfg, *, euid: None)
    _instantane(cfg_valide.src, "20260820-093240")
    run(cfg_valide, _runner_sain(), dry_run=False, now=time.time())
    assert re.search(r"terminé en \d+s — 1 instantané\(s\) en ligne",
                     capsys.readouterr().out)


def test_la_duree_figure_aussi_sur_un_verdict_negatif(monkeypatch, cfg_valide, capsys):
    """Un échec est justement le moment où l'on veut savoir combien de temps
    ça a duré avant de renoncer."""
    import re

    monkeypatch.setattr("pgtool.offsite.preflight", lambda cfg, *, euid: None)
    _instantane(cfg_valide.src, "20260820-093240")
    r = _runner_sain()
    r.matchers.insert(0, (lambda argv: "lsf" in argv and "-R" in argv,
                          Result(("rclone",), 1, "", "refus")))
    assert run(cfg_valide, r, dry_run=False, now=time.time()) == EXIT_FAILED
    assert re.search(r"terminé en \d+s — 1 instantané\(s\) en échec",
                     capsys.readouterr().err)
