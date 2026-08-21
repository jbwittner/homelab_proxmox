"""Où `fj deploy` lit ce qu'il pose.

DÉFAUT CONSTATÉ LE 21 AOÛT 2026, sur `pve-eranikus`, depuis le répertoire du
service :

    root@pve-eranikus:~/homelab_proxmox/pve-eranikus/forgejo# fj deploy --dry-run
    [ERROR] /usr/local/lib/fjtool ne ressemble pas au service Forgejo du dépôt
            (ct/app.ini introuvable) — jouer fj depuis le dépôt, ou préciser --src

L'opérateur ÉTAIT dans le dépôt. Le message lui disait d'y aller.

La cause : `fj` tapé sans chemin résout par le `PATH` vers `/usr/local/sbin/fj`,
la copie **installée**, qui déduisait la racine du service de sa propre
position — `/usr/local/lib/fjtool`. Le répertoire courant, lui, était le bon et
n'était jamais regardé.

Deux corrections, et la seconde compte autant que la première : le répertoire
courant devient un candidat, et le refus nomme **tout ce qui a été essayé**.
Un message qui envoie là où l'on se trouve déjà est pire qu'un message absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fjtool.cli import racine_du_service
from fjtool.location import Refus


def _faux_service(racine: Path) -> Path:
    """Un répertoire qui a la forme d'un service Forgejo du dépôt."""
    (racine / "ct").mkdir(parents=True)
    (racine / "ct" / "app.ini").write_text("APP_NAME = Forgejo\n")
    return racine


def test_le_premier_candidat_valable_gagne(tmp_path):
    depot = _faux_service(tmp_path / "depot")
    autre = _faux_service(tmp_path / "autre")
    assert racine_du_service([depot, autre]) == depot


def test_un_candidat_sans_ct_app_ini_est_ecarte(tmp_path):
    """C'est le cas de la copie installée : `/usr/local/lib/fjtool` porte le
    code, jamais la charge utile du montage."""
    installee = tmp_path / "usr-local-lib-fjtool"
    (installee / "fjtool").mkdir(parents=True)
    depot = _faux_service(tmp_path / "depot")
    assert racine_du_service([installee, depot]) == depot


def test_le_repertoire_courant_est_un_candidat(tmp_path, monkeypatch):
    """LE défaut du 21 août 2026, reproduit.

    La copie installée est essayée d'abord et écartée ; le répertoire courant
    doit alors prendre le relais, parce que c'est exactement là que se tient
    l'opérateur quand il tape `fj deploy`.
    """
    from fjtool import cli

    installee = tmp_path / "usr-local-lib-fjtool"
    (installee / "fjtool").mkdir(parents=True)
    depot = _faux_service(tmp_path / "depot")

    monkeypatch.chdir(depot)
    import argparse

    args = argparse.Namespace(src=None)
    assert cli._source_du_depot(args, module=installee / "fjtool" / "cli.py") == depot


def test_aucun_candidat_valable_est_un_refus(tmp_path):
    vide = tmp_path / "rien"
    vide.mkdir()
    with pytest.raises(Refus):
        racine_du_service([vide])


def test_le_refus_nomme_TOUT_ce_qui_a_ete_essaye(tmp_path):
    """Le cœur du défaut. Un refus qui ne cite qu'un chemin laisse croire que
    c'est le seul qui comptait — et si ce chemin est celui de la copie
    installée, l'opérateur ne comprend pas pourquoi son répertoire courant,
    parfaitement valable, n'a pas été retenu."""
    a = tmp_path / "un"
    b = tmp_path / "deux"
    a.mkdir()
    b.mkdir()
    with pytest.raises(Refus) as capture:
        racine_du_service([a, b])
    message = str(capture.value)
    assert str(a) in message
    assert str(b) in message


def test_le_refus_dit_quoi_taper(tmp_path):
    vide = tmp_path / "rien"
    vide.mkdir()
    with pytest.raises(Refus) as capture:
        racine_du_service([vide])
    assert "--src" in str(capture.value)


def test_le_depot_reel_est_reconnu(depot_forgejo):
    """Le service livré doit passer par le même chemin que n'importe quel
    autre — sinon le contrôle ne prouve rien sur la production."""
    assert racine_du_service([depot_forgejo]) == depot_forgejo
