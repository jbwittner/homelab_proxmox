"""Journalisation — format identique à celui de pg-backup.sh (bash).

Les journaux du CT et de l'hôte doivent rester corrélables dans journalctl :
le format ne change pas parce que le langage change.
"""

from __future__ import annotations

import sys
from datetime import datetime


def _emit(level: str, message: str, stream=sys.stdout) -> None:
    print(f"{datetime.now():%H:%M:%S} [{level}] {message}", file=stream, flush=True)


def step(message: str) -> None:
    _emit("STEP ", message)


def info(message: str) -> None:
    _emit("INFO ", message)


def warn(message: str) -> None:
    _emit("WARN ", message, sys.stderr)


def error(message: str) -> None:
    _emit("ERROR", message, sys.stderr)