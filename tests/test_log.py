"""Le format de journalisation est un contrat entre deux langages.

`pg-backup.sh` reste en bash. Ses lignes et celles du Python atterrissent dans
le même `journalctl -u` : si l'un des deux dérive, la corrélation se perd sans
que rien ne casse. D'où un test qui lit le bash et compare.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core import log

REPO = Path(__file__).resolve().parent.parent
BASH = REPO / "pve-eranikus" / "pgsql" / "ct" / "pg-backup.sh"

LIGNE = re.compile(r"^(\d{2}):(\d{2}):(\d{2}) \[(.{5})\] (.*)$")


def _seule(capsys, flux: str) -> str:
    captured = capsys.readouterr()
    texte = getattr(captured, flux)
    assert texte.endswith("\n")
    return texte[:-1]


@pytest.mark.parametrize(
    "fonction, niveau, flux",
    [
        (log.step, "STEP ", "out"),
        (log.info, "INFO ", "out"),
        (log.warn, "WARN ", "err"),
        (log.error, "ERROR", "err"),
    ],
)
def test_forme_de_ligne(capsys, fonction, niveau, flux):
    """Heure seule, niveau sur 5 caractères, message tel quel."""
    fonction("un message")
    ligne = _seule(capsys, flux)
    m = LIGNE.match(ligne)
    assert m, f"forme inattendue : {ligne!r}"
    assert m.group(4) == niveau
    assert m.group(5) == "un message"


def test_les_flux_sont_separes_comme_en_bash(capsys):
    """step/info sur stdout, warn/error sur stderr — comme le bash."""
    log.step("s")
    log.info("i")
    log.warn("w")
    log.error("e")
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 2
    assert captured.err.count("\n") == 2
    assert "[STEP ]" in captured.out and "[INFO ]" in captured.out
    assert "[WARN ]" in captured.err and "[ERROR]" in captured.err


def test_continuation_alignee_sous_le_niveau():
    """9 espaces = largeur de « HH:MM:SS ». C'est ce qui aligne une ligne de
    continuation sous la colonne du niveau."""
    assert log.CONT == " " * len("HH:MM:SS ")


def test_detail_recopie_sans_horodatage(capsys):
    log.detail("première\nseconde")
    lignes = capsys.readouterr().out.splitlines()
    assert lignes == [f"{log.CONT}première", f"{log.CONT}seconde"]
    for ligne in lignes:
        assert not LIGNE.match(ligne), "un détail ne doit pas être horodaté"


@pytest.mark.skipif(not BASH.exists(), reason="pg-backup.sh introuvable")
def test_identique_au_bash():
    """Les niveaux déclarés par le bash sont exactement ceux du Python.

    Si quelqu'un modifie l'un des deux côtés, ce test tombe — c'est tout son
    objet.
    """
    source = BASH.read_text(encoding="utf-8")
    trouves = set(re.findall(r"printf '%s \[(.{5})\] %s\\n'", source))
    assert trouves == {"INFO ", "WARN ", "ERROR", "STEP "}, trouves

    # Et l'horodatage : « date '+%H:%M:%S' », l'heure seule — journald ajoute
    # la date, la remettre ici doublerait l'information dans chaque ligne.
    assert "date '+%H:%M:%S'" in source
    assert "date '+%Y" not in source
