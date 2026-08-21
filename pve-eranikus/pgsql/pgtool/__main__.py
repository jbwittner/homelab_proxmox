"""Permet `python3 -m pgtool`, utile en développement et dans les tests.

L'exécutable installé est `pg` (voir ../pg), qui contrôle la version de Python
avant d'arriver ici.
"""

from __future__ import annotations

import sys

from pgtool.cli import main

if __name__ == "__main__":
    sys.exit(main())
