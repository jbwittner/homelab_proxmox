"""Où tourne-t-on, et sur quel conteneur.

`fj` est un outil de NŒUD, et rien que de nœud. Il ne se pousse pas dans le
conteneur et n'y délègue aucune commande : tout ce qu'il fait, il le fait par
`pct exec`.

Ce ne fut pas toujours le cas. Une version antérieure poussait `fjtool` dans le
CT 400 pour y faire tourner `fj backup` et `fj list` — sauvegarde locale et
copie hors-site propres au conteneur. Ces commandes ont disparu quand la base
est devenue un locataire du CT 200 : c'est `pg` qui sauvegarde la base, et
`vzdump` qui emporte les dépôts. Sans commande à exécuter là-bas, il n'y a plus
de moteur à y déposer, plus d'arbre d'import à y synchroniser, et plus de
frontière à faire traverser à des variables d'environnement.

Ce qui reste tient en trois fonctions : dire où l'on est, lire le CTID
consigné, et refuser proprement.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Mapping

from core.runner import Runner

# Le CTID est consigné à un seul endroit, écrit par `fj deploy`.
CONF = Path("/etc/default/fjbk")


class Where(Enum):
    HOST = "nœud"
    CONTAINER = "conteneur"


class Refus(RuntimeError):
    """Refus argumenté : un message, et un code de retour 1."""


def detect(runner: Runner) -> Where:
    """Sur la présence de `pct` — un nœud Proxmox l'a, un conteneur Debian non.

    `fj` n'a plus rien à faire dans un conteneur ; cette fonction sert donc à
    REFUSER de continuer si on l'y lance par erreur, et non à répartir du
    travail. Un `pct exec 400 -- fj deploy` échouerait autrement sur un
    « pct: command not found » qui ne dit pas ce qui se passe.
    """
    return Where.HOST if runner.which("pct") else Where.CONTAINER


def exiger_le_noeud(runner: Runner) -> None:
    if detect(runner) is Where.CONTAINER:
        raise Refus(
            "fj est un outil du NŒUD : il lui faut `pct`. Le jouer depuis le "
            "dépôt sur pve-eranikus, pas dans le conteneur."
        )


def read_conf(chemin: Path = CONF) -> dict[str, str]:
    """Lit un fichier `CLÉ=valeur`, sans shell.

    On analyse plutôt que de `source` : un fichier de configuration n'a pas à
    pouvoir lancer des commandes.
    """
    valeurs: dict[str, str] = {}
    if not chemin.is_file():
        return valeurs
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        cle, _, valeur = ligne.partition("=")
        valeurs[cle.strip()] = valeur.strip().strip('"').strip("'")
    return valeurs


def resolve_ctid(
    *,
    flag: str | None,
    env: Mapping[str, str],
    conf: Mapping[str, str],
    defaut: int | None = None,
) -> int:
    """`--ctid`, puis l'environnement, puis le fichier.

    Sans `defaut`, l'absence est un refus : deviner un CTID, c'est risquer de
    déployer la source de vérité dans le mauvais conteneur.

    Seul `fj deploy` en passe un, et pour une raison précise : il doit pouvoir
    amorcer une installation vierge, où `/etc/default/fjbk` n'existe pas encore
    puisque c'est LUI qui l'écrit.
    """
    brut = flag or env.get("FJ_CTID") or conf.get("FJ_CTID") or ""
    if not brut and defaut is not None:
        return defaut
    if not brut:
        raise Refus(
            f"aucun conteneur cible : {CONF} absent ou sans FJ_CTID\n"
            "         le consigner  : fj deploy --ctid <ID>\n"
            "         ou ponctuel   : fj --ctid <ID> <commande>"
        )
    if not re.fullmatch(r"[0-9]+", brut):
        raise Refus(f"CTID invalide : {brut}")
    return int(brut)
