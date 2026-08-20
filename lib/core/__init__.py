"""Briques génériques, sans aucune connaissance de Proxmox ni d'un service.

Ce paquet est le SEUL qui soit poussé dans les conteneurs. Deux règles en
découlent, et elles se vérifient mécaniquement (voir tests/) :

  - il n'importe jamais `proxmox` — un conteneur n'a rien à faire avec `pct`,
    et le paquet n'y est même pas déposé ;
  - il ne nomme aucun service. Si « postgres » apparaît ici, le code est au
    mauvais endroit.

Bibliothèque standard uniquement : rien à installer sur l'hyperviseur ni dans
un conteneur.
"""

from __future__ import annotations

import sys

MIN_PYTHON = (3, 11)


def require_python(minimum: tuple[int, int] = MIN_PYTHON) -> None:
    """Refuse de démarrer sous une version trop ancienne.

    `python3` vient du template Debian, pas d'une décision explicite : rien ne
    garantit sa version sur un conteneur recréé autrement. Mieux vaut un refus
    net en tête d'exécutable qu'une `SyntaxError` au milieu d'une restauration.

    À appeler depuis le point d'entrée AVANT d'importer quoi que ce soit qui
    dépende d'une syntaxe récente.
    """
    if sys.version_info < minimum:
        found = ".".join(str(n) for n in sys.version_info[:3])
        wanted = ".".join(str(n) for n in minimum)
        raise SystemExit(
            f"python3 {wanted} minimum requis, {found} trouvé "
            f"({sys.executable}). Installer python3 dans le conteneur."
        )
