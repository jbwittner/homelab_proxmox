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

# Les outils propres à un service vivent dans le répertoire de ce service, et
# sont déposés dans le conteneur à côté de `core`. On reproduit donc la même
# disposition : `lib/` puis le répertoire du service, tous deux à plat.
SERVICES = [
    REPO / "pve-eranikus" / "pgsql",
    REPO / "pve-eranikus" / "forgejo",
]

for chemin in [LIB, *SERVICES]:
    if str(chemin) not in sys.path:
        sys.path.insert(0, str(chemin))


import pytest  # noqa: E402 - après la mise en place du chemin d'import


@pytest.fixture
def depot_forgejo() -> Path:
    """La racine du service Forgejo DANS LE DÉPÔT.

    Plusieurs contrôles confrontent le code aux fichiers réellement livrés —
    `ct/VERSION`, `ct/app.ini`, les unités. Recalculer ce chemin dans chaque
    fichier de test finirait par produire deux vérités.
    """
    return REPO / "pve-eranikus" / "forgejo"
