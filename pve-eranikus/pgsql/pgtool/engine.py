"""Le moteur, côté conteneur : ce qu'on fait des instantanés.

Ce module tourne DANS le CT. Il ne connaît ni `pct` ni Proxmox — il ne voit
que `/var/backups/postgresql`, un cluster PostgreSQL sur sa socket locale, et
`systemd`.

`delete` est la seule commande qui détruit, et toute sa valeur est dans ses
refus. Deux d'entre eux méritent d'être compris :

  - **la protection porte sur la RÉFÉRENCE RÉSOLUE, pas sur ce qui a été
    tapé.** « 20260820 » désigne la plus récente de ce jour-là, qui peut être
    le dernier instantané. Juger avant de résoudre laisserait passer
    exactement le cas qu'on veut interdire ;
  - **le dernier instantané est protégé sans échappatoire.** Le supprimer
    laisserait le cluster sans filet, et l'unique moment où l'on s'en aperçoit
    est celui où l'on en aurait eu besoin.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

from core.log import CONT, detail, info, step, warn
from pgtool.snapshots import Snapshot, Store

KIO = 1024
UNITES = ("", "K", "M", "G", "T", "P")


class DeleteRefused(RuntimeError):
    """Une garde a parlé. Rien n'a été supprimé."""


def human_size(octets: int) -> str:
    """Comme `du -h` : une décimale en dessous de 10, un entier au-delà, et
    l'arrondi TOUJOURS AU-DESSUS.

    `du` ne fait pas un arrondi au plus proche, il compte des unités entamées :
    1025 octets s'affichent « 1.1K » et non « 1.0K », 10241 donnent « 11K ».
    Arrondir au plus proche fait diverger les deux sorties dès qu'une taille
    tombe du mauvais côté — c'est ce qui donnait 33K là où le bash disait 34K.

    Les divisions par 1024 sont exactes en binaire, il n'y a donc pas de
    question d'epsilon : le plafond est calculé sur une valeur juste.
    """
    valeur = float(octets)
    for unite in UNITES:
        if valeur < KIO or unite == UNITES[-1]:
            if not unite:
                return str(int(valeur))
            if valeur < 10:
                return f"{math.ceil(valeur * 10) / 10:.1f}{unite}"
            return f"{math.ceil(valeur)}{unite}"
        valeur /= KIO
    return str(octets)  # pragma: no cover - inatteignable


def free_mb(chemin: Path) -> int:
    """Mégaoctets disponibles, au sens de `df -m`.

    Deux détails, et les deux comptent pour que les sorties restent
    comparables à celles du bash :

      - « disponible » et non « libre » : `shutil.disk_usage().free` se fonde
        sur les blocs disponibles à l'utilisateur, hors blocs réservés, comme
        la colonne de `df` ;
      - l'arrondi se fait AU-DESSUS. `df -m` compte des unités entamées ;
        tronquer donne systématiquement une unité de moins.
    """
    mio = KIO * KIO
    return -(-shutil.disk_usage(chemin).free // mio)


# ─── delete ──────────────────────────────────────────────────────────────────


def plan_delete(store: Store, ref: str) -> Snapshot:
    """Applique TOUTES les gardes et ne supprime rien. Renvoie la cible.

    C'est le contrat de `--plan`, et c'est ce qui permet à la question de
    confirmation — posée sur le nœud, où il y a un terminal — de porter sur ce
    qui sera réellement supprimé.
    """
    if ref == "latest":
        raise DeleteRefused(
            "le dernier instantané est protégé — "
            "il n'y a rien à supprimer sous ce nom"
        )

    # Résoudre AVANT de juger : c'est la résolution qui révèle qu'une date
    # seule désigne peut-être l'instantané protégé.
    vise = store.resolve(ref)

    dernier = store.latest()
    if dernier is not None and vise.path == dernier.path:
        raise DeleteRefused(
            f"{vise.name} est le dernier instantané — protégé.\n"
            f"{CONT}Supprimer la dernière sauvegarde laisserait le cluster "
            "sans filet.\n"
            f"{CONT}Lancer « pg backup » d'abord si le but est de la remplacer."
        )
    return vise


def describe_delete(vise: Snapshot) -> str:
    """Le détail humain qui accompagne un `--plan`.

    Part sur la sortie d'ERREUR : la sortie standard ne porte que le nom
    résolu, parce que le nœud la lit pour formuler sa question.
    """
    lignes = [f"instantané visé : {vise.name}"]
    for cle, valeur in vise.manifest().items():
        lignes.append(f"{CONT}{cle:<12}: {valeur}")
    lignes.append(f"{CONT}taille : {human_size(vise.size_bytes())}")
    return "\n".join(lignes)


def do_delete(store: Store, vise: Snapshot) -> None:
    """Supprime, puis dit ce qui reste. À n'appeler qu'après `plan_delete`."""
    shutil.rmtree(vise.path)
    step(f"supprimé : {vise.name}")
    info(
        f"  {len(store.snapshots())} sauvegarde(s) restante(s), "
        f"{free_mb(store.dest)} Mo libres"
    )


# ─── affichage ───────────────────────────────────────────────────────────────

# Colonnes du bash. Le remplissage se fait ici en CARACTÈRES et non en octets :
# `printf '%-18s'` compte des octets, si bien que l'en-tête accentué
# « INSTANTANÉ » ressortait décalé d'une colonne. La différence d'un espace sur
# la ligne d'en-tête est le seul écart voulu avec la sortie bash.
LARGEURS = (18, 10, 8)


def _ligne(nom: str, age: str, taille: str, bases: str, marque: str = "") -> str:
    n, a, t = LARGEURS
    return f"{nom:<{n}}  {age:<{a}}  {taille:<{t}}  {bases}{marque}"


def render_list(store: Store, *, maintenant: float) -> str:
    instantanes = store.snapshots()
    if not instantanes:
        return f"aucune sauvegarde dans {store.dest}"

    dernier = instantanes[-1]
    lignes = [
        _ligne("INSTANTANÉ", "ÂGE", "TAILLE", "BASES"),
        _ligne("-" * 18, "-" * 10, "-" * 8, "-----"),
    ]
    total = 0
    for s in instantanes:
        octets = s.size_bytes()
        total += octets
        lignes.append(_ligne(
            s.name,
            f"{s.age_days(maintenant=maintenant)}j",
            human_size(octets),
            " ".join(s.databases),
            " ← latest" if s.path == dernier.path else "",
        ))
    return "\n".join(lignes)


def list_summary(store: Store) -> str:
    """La ligne de bilan, SÉPARÉE du tableau.

    Le tableau est une donnée : il se recopie tel quel, sans horodatage. Le
    bilan est un message sur cette donnée, il passe donc par la journalisation
    et porte l'heure et son niveau — la distinction posée dans `core.log`, et
    celle que le bash faisait déjà en n'horodatant que cette ligne-là.
    """
    instantanes = store.snapshots()
    total = sum(s.size_bytes() for s in instantanes)
    return (
        f"{len(instantanes)} sauvegarde(s), {human_size(total)} — "
        f"{free_mb(store.dest)} Mo libres"
    )


def render_show(store: Store, ref: str = "latest") -> str:
    instantane = store.resolve(ref)
    lignes = [f"instantané : {instantane.name}"]
    manifeste = instantane.manifest()
    if manifeste:
        lignes += [f"{CONT}{cle:<12}: {valeur}"
                   for cle, valeur in manifeste.items()]
    else:
        lignes.append(f"{CONT}pas de MANIFEST — instantané incomplet ?")
    lignes.append("fichiers :")
    for nom in instantane.files:
        octets = (instantane.path / nom).stat().st_size
        lignes.append(f"{CONT}{nom:<24} {human_size(octets)}")
    if not instantane.has_globals:
        lignes.append(f"{CONT}globals.sql absent — les rôles ne sont pas dans "
                      "cet instantané")
    return "\n".join(lignes)
