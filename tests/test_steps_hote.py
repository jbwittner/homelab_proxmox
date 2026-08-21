"""Les sections de l'hôte, portées en `Step` : binaires et paquets.

Les plus mécaniques d'abord, comme prévu au plan. Chaque test porte sur une
DÉCISION — ce que l'étape constate et ce qu'elle propose de faire — jamais sur
l'exécution, qui est générique et testée dans `test_converge.py`.
"""

from __future__ import annotations

import os
import stat

import pytest

from core.converge import Context, Mode
from core.runner import FakeRunner, Fs, Result
from pgtool.deploy import Options, Paths, contexte
from pgtool.steps.hote import PgHote, PgbkHote, PgtoolHote, Python3Hote, Rclone


@pytest.fixture
def depot(tmp_path):
    """Un dépôt minimal : ce que les étapes de l'hôte vont y chercher."""
    service = tmp_path / "depot" / "pve-eranikus" / "pgsql"
    (service / "ct").mkdir(parents=True)
    (service / "host").mkdir()
    (service / "pgtool").mkdir()
    (tmp_path / "depot" / "lib" / "core").mkdir(parents=True)
    (tmp_path / "depot" / "lib" / "proxmox").mkdir(parents=True)

    (service / "ct" / "pgbk.sh").write_text("#!/bin/bash\n# moteur\n")
    (service / "pg").write_text("#!/usr/bin/python3\n# lanceur\n")
    (service / "pgtool" / "cli.py").write_text("# cli\n")
    (tmp_path / "depot" / "lib" / "core" / "log.py").write_text("# log\n")
    (tmp_path / "depot" / "lib" / "proxmox" / "__init__.py").write_text("# pve\n")
    return service


@pytest.fixture
def ctx(depot, tmp_path, monkeypatch):
    racine = tmp_path / "cible"
    (racine / "usr" / "local" / "sbin").mkdir(parents=True)
    (racine / "usr" / "local" / "lib").mkdir(parents=True)

    c = contexte(
        runner=FakeRunner(),
        paths=Paths(
            src=depot,
            host_pgbk=racine / "usr/local/sbin/pgbk",
            host_pg=racine / "usr/local/sbin/pg",
            host_lib=racine / "usr/local/lib/pgtool",
        ),
        opts=Options(ctid=200),
        mode=Mode.APPLY,
    )
    return c


def _appliquer(etape, ctx):
    """Constate puis joue le plan, comme le fait le parcours."""
    resultat = etape.check(ctx)
    for action in resultat.actions:
        action.run(ctx)
    return resultat


# ─── python3 sur l'hôte ──────────────────────────────────────────────────────


def test_python3_present_et_assez_recent(ctx, monkeypatch):
    monkeypatch.setattr("pgtool.steps.hote._version_python",
                        lambda chemin: (3, 13))
    resultat = Python3Hote().check(ctx)
    assert resultat.state == "ok"
    assert "3.13" in resultat.detail


def test_python3_trop_ancien_est_une_erreur_pas_une_pose(ctx, monkeypatch):
    """On ne « pose » pas une version de Python : le dire et s'arrêter là vaut
    mieux qu'une action qu'on ne saurait pas écrire."""
    monkeypatch.setattr("pgtool.steps.hote._version_python",
                        lambda chemin: (3, 9))
    resultat = Python3Hote().check(ctx)
    assert resultat.state == "error"
    assert resultat.actions == ()
    assert "3.11" in resultat.detail


def test_python3_absent_est_une_erreur(ctx, monkeypatch):
    monkeypatch.setattr("pgtool.steps.hote._version_python", lambda chemin: None)
    resultat = Python3Hote().check(ctx)
    assert resultat.state == "error"
    assert resultat.actions == ()


# ─── rclone ──────────────────────────────────────────────────────────────────


def test_rclone_present_ne_propose_rien(ctx, tmp_path):
    binaire = tmp_path / "rclone"
    binaire.write_text("#!/bin/sh\n")
    binaire.chmod(binaire.stat().st_mode | stat.S_IEXEC)
    resultat = Rclone(binaire).check(ctx)
    assert resultat.state == "ok"
    assert resultat.actions == ()


def test_rclone_absent_propose_une_installation(ctx, tmp_path):
    resultat = Rclone(tmp_path / "nexiste-pas").check(ctx)
    assert resultat.state == "absent"
    assert any("apt-get" in a.label for a in resultat.actions)


def test_rclone_absent_sans_install_est_une_erreur(ctx, tmp_path):
    """« Ne jamais armer un automatisme dont les prérequis manquent » : le dire
    et ne rien poser vaut mieux qu'un timer qui échouera à 3h30."""
    ctx.opts = Options(ctid=200, do_install=False)
    resultat = Rclone(tmp_path / "nexiste-pas").check(ctx)
    assert resultat.state == "error"
    assert resultat.actions == ()


def test_rclone_est_saute_sans_hors_site(ctx, tmp_path):
    """rclone n'est une dépendance que de la copie hors-site."""
    ctx.opts = Options(ctid=200, do_offsite=False)
    assert Rclone(tmp_path / "x").skip_if(ctx) is not None


# ─── pgbk et pg sur l'hôte ───────────────────────────────────────────────────


def test_un_binaire_absent_est_a_poser(ctx):
    resultat = PgbkHote().check(ctx)
    assert resultat.state == "absent"
    assert len(resultat.actions) == 1


def test_un_binaire_conforme_ne_propose_rien(ctx):
    _appliquer(PgbkHote(), ctx)
    assert PgbkHote().check(ctx).state == "ok"


def test_un_binaire_pose_a_le_bon_mode(ctx):
    _appliquer(PgbkHote(), ctx)
    mode = stat.S_IMODE(ctx.paths.host_pgbk.stat().st_mode)
    assert mode == 0o755, "le montage est en lecture seule, d'où la copie"


def test_un_contenu_different_est_une_derive(ctx):
    _appliquer(PgbkHote(), ctx)
    ctx.paths.host_pgbk.write_text("#!/bin/bash\n# autre chose\n")
    assert PgbkHote().check(ctx).state == "drift"


def test_un_mode_different_est_une_derive(ctx):
    """Un fichier au bon contenu mais sans le bit d'exécution ne s'exécute
    pas ; le constater est le rôle de cette étape."""
    _appliquer(PgbkHote(), ctx)
    ctx.paths.host_pgbk.chmod(0o644)
    assert PgbkHote().check(ctx).state == "drift"


def test_pgbk_de_lhote_vient_de_ct(ctx):
    """Un seul fichier, deux rôles : l'hôte le lit à travers la frontière."""
    resultat = PgbkHote().check(ctx)
    assert "ct/pgbk.sh" in resultat.actions[0].label


def test_le_lanceur_pg_est_pose_en_755(ctx):
    _appliquer(PgHote(), ctx)
    assert stat.S_IMODE(ctx.paths.host_pg.stat().st_mode) == 0o755


# ─── l'arbre d'import ────────────────────────────────────────────────────────


def test_larbre_absent_est_a_poser(ctx):
    resultat = PgtoolHote().check(ctx)
    assert resultat.state == "absent"
    assert resultat.actions


def test_larbre_pose_contient_les_trois_paquets(ctx):
    _appliquer(PgtoolHote(), ctx)
    poses = {p.name for p in ctx.paths.host_lib.iterdir()}
    assert poses == {"core", "proxmox", "pgtool"}


def test_larbre_conforme_ne_propose_rien(ctx):
    _appliquer(PgtoolHote(), ctx)
    assert PgtoolHote().check(ctx).state == "ok"


def test_un_module_modifie_est_une_derive(ctx):
    _appliquer(PgtoolHote(), ctx)
    (ctx.paths.host_lib / "core" / "log.py").write_text("# modifié\n")
    assert PgtoolHote().check(ctx).state == "drift"


def test_un_module_que_le_depot_na_plus_est_retire(ctx):
    """Sans élagage, un module renommé laisse son ancêtre, qui continue de
    s'importer : le nœud tournerait sur du code absent du dépôt."""
    _appliquer(PgtoolHote(), ctx)
    orphelin = ctx.paths.host_lib / "core" / "ancien.py"
    orphelin.write_text("# vestige\n")

    resultat = PgtoolHote().check(ctx)
    assert resultat.state == "drift"
    assert any("ancien.py" in a.label for a in resultat.actions)

    for action in resultat.actions:
        action.run(ctx)
    assert not orphelin.exists()


def test_larbre_de_lhote_porte_proxmox(ctx):
    """Contrairement à celui du conteneur : le nœud en a besoin, le CT non."""
    _appliquer(PgtoolHote(), ctx)
    assert (ctx.paths.host_lib / "proxmox").is_dir()
