"""Journalisation — format identique à celui de pg-backup.sh (bash).

Les journaux du CT et de l'hôte doivent rester corrélables dans journalctl :
le format ne change pas parce que le langage change. `pg-backup.sh` reste en
bash et continue d'émettre exactement ces lignes-là ; toute dérive ici les
désolidariserait dans un même `journalctl -u`.

Le contrat, tel que bash l'écrit :

    printf '%s [INFO ] %s\\n' "$(date '+%H:%M:%S')" "$*"

soit l'heure SEULE (pas la date, journald l'ajoute), un niveau de 5 caractères
entre crochets — d'où l'espace de complément de `INFO `, `WARN ` et `STEP ` —
puis le message. `step` et `info` sortent sur stdout, `warn` et `error` sur
stderr, comme en bash.
"""

from __future__ import annotations

import sys
from datetime import datetime

# Largeur de « HH:MM:SS » plus l'espace qui suit : de quoi aligner une ligne de
# continuation sous la colonne du niveau. Bash fait la même chose à la main
# (`sed 's/^/         /'`, `awk '{print "         " $0}'`) ; ici c'est nommé
# une fois, sinon l'alignement se perd à la première recopie.
CONT = " " * 9


def _emit(level: str, message: str, stream=None) -> None:
    # Le flux est résolu à l'APPEL, pas à l'import : le lier une fois pour
    # toutes rendrait toute redirection de sys.stdout sans effet — y compris
    # celle d'un banc d'essai, qui ne verrait alors plus rien.
    print(
        f"{datetime.now():%H:%M:%S} [{level}] {message}",
        file=stream if stream is not None else sys.stdout,
        flush=True,
    )


def step(message: str) -> None:
    _emit("STEP ", message)


def info(message: str) -> None:
    _emit("INFO ", message)


def warn(message: str) -> None:
    _emit("WARN ", message, sys.stderr)


def error(message: str) -> None:
    _emit("ERROR", message, sys.stderr)


def detail(text: str, *, stream=None) -> None:
    """Recopie un texte multiligne sous la colonne du niveau.

    Pour la sortie d'une commande ou le contenu d'un fichier : ce sont des
    données, pas des événements, et elles ne prennent donc pas d'horodatage —
    exactement ce que fait bash en préfixant neuf espaces.
    """
    cible = stream if stream is not None else sys.stdout
    for line in text.splitlines():
        print(f"{CONT}{line}", file=cible, flush=True)
