"""Socle des tests — aucune infrastructure, aucun réseau, aucun conteneur.

`lib/` est mis sur le chemin d'import, ce qui fait de `core` et `proxmox` des
paquets de premier niveau. C'est exactement la disposition qu'aura le
conteneur, qui ne reçoit que `core/` : les tests exercent donc le même mode
d'import que la production, et non un montage propre au banc d'essai.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"

if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))
