"""Permet `python3 -m fjtool`, utile en développement et dans les tests.

L'exécutable installé est `fj` (voir ../fj), qui contrôle la version de Python
avant d'arriver ici.
"""

from __future__ import annotations

import sys

from fjtool.cli import main

if __name__ == "__main__":
    sys.exit(main())
