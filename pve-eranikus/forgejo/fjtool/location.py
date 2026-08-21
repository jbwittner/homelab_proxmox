"""Où tourne-t-on, et à qui déléguer.

Le même exécutable sert des deux côtés du montage : sur le nœud il achemine,
dans le conteneur il travaille. Cette décision gouverne tout le reste, d'où un
module à part, lisible et testable seul.

`pct exec` n'alloue pas de TTY. Une question posée depuis le conteneur ne
verrait jamais la saisie : les confirmations se posent donc **côté hôte**, là
où le terminal existe, et le conteneur reçoit `--yes`.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

from core.runner import CommandError, Runner

# Chemin du moteur DANS le conteneur. Absolu : le PATH de `pct exec` est
# minimal et n'inclut pas /usr/local/bin.
CT_FJ = "/usr/local/bin/fj"

# Le CTID est consigné à un seul endroit, écrit par `fj deploy`.
CONF = Path("/etc/default/fjbk")


class Where(Enum):
    HOST = "nœud"
    CONTAINER = "conteneur"


class Refus(RuntimeError):
    """Refus argumenté : un message, et un code de retour 1."""


def detect(runner: Runner) -> Where:
    """Sur la présence de `pct` — un nœud Proxmox l'a, un conteneur Debian non."""
    return Where.HOST if runner.which("pct") else Where.CONTAINER


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
    restaurer la source de vérité dans le mauvais conteneur.

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


# Ce qui traverse la frontière du conteneur, et RIEN D'AUTRE.
#
# `pct exec` n'hérite d'aucun environnement : une variable posée sur le nœud
# est silencieusement perdue — et « silencieusement » est le mot qui compte,
# la commande réussit, elle fait simplement autre chose que ce qu'on a demandé.
# Le CT 200 a payé ce défaut pendant son exercice de bascule :
# « PG_BACKUP_DEST=/tmp/pra pg restore pra » tapé depuis le nœud visait le
# dépôt de PRODUCTION.
#
# Une liste explicite plutôt que tout l'environnement : recopier le nôtre
# porterait des secrets du nœud dans le conteneur, et les rendrait visibles
# dans un `ps`.
VARIABLES_TRANSMISES = ("FJ_BACKUP_DEST",)


@dataclass(frozen=True)
class Delegate:
    """Achemine une commande vers le moteur du conteneur.

    Tout ce qui peut manquer est constaté AVANT de déléguer, avec un message
    qui dit quoi faire — plutôt qu'un « command not found » venu de l'autre
    côté du montage, que rien ne rattache à sa cause.
    """

    runner: Runner
    ctid: int

    def preflight(self) -> None:
        if not self.runner.probe("pct", "config", str(self.ctid)):
            raise Refus(f"CT {self.ctid} inexistant")
        etat = self.runner.read("pct", "status", str(self.ctid), check=False)
        if etat.out.split()[-1:] != ["running"]:
            raise Refus(
                f"CT {self.ctid} à l'arrêt — le démarrer : pct start {self.ctid}"
            )
        if not self.runner.probe(
            "pct", "exec", str(self.ctid), "--", "test", "-x", CT_FJ
        ):
            raise Refus(
                f"{CT_FJ} absent du CT {self.ctid} — le poser : fj deploy"
            )

    def _argv(
        self,
        commande: str,
        args: Sequence[str],
        *extra: str,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        """L'argv réel, avec ce qui doit traverser la frontière.

        `env` en préfixe, jamais une chaîne shell : `pct exec` transmet un argv
        qu'il n'interprète pas, et `env` est le moyen POSIX de poser des
        variables devant une commande sans passer par un interpréteur.
        """
        passage = [
            f"{nom}={valeur}"
            for nom in VARIABLES_TRANSMISES
            # Une valeur vide vaut « non posée ». La transmettre écraserait le
            # défaut du moteur par une chaîne vide — et un chemin vide résout
            # en répertoire courant.
            if (valeur := (env or {}).get(nom))
        ]
        prefixe = ["env", *passage] if passage else []
        return [
            "pct", "exec", str(self.ctid), "--", *prefixe,
            CT_FJ, commande, *args, *extra,
        ]

    def plan(
        self,
        commande: str,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
    ) -> str:
        """Le nom réellement visé, sans rien effacer.

        Contrat du moteur : la sortie standard ne porte QUE ce nom ; le détail
        humain part sur la sortie d'erreur.
        """
        try:
            res = self.runner.read(*self._argv(commande, args, "--plan", env=env))
        except CommandError as exc:
            # Recopié VERBATIM. Le moteur formate déjà ses lignes
            # (« HH:MM:SS [ERROR] … ») ; les repasser par error() les
            # préfixerait une seconde fois, et le journal porterait deux
            # horodatages sur la même ligne — défaut constaté sur `pg` le
            # 21 août 2026. La façade achemine, elle ne réécrit pas.
            sortie = exc.result.stderr.rstrip("\n")
            if sortie:
                print(sortie, file=sys.stderr, flush=True)
            raise Refus("") from exc
        return res.out

    def hand_over(
        self,
        commande: str,
        args: Sequence[str],
        *,
        yes: bool,
        env: Mapping[str, str] | None = None,
    ) -> None:
        """Remplace ce processus par la commande du conteneur.

        Le terminal, l'entrée standard et le code de retour passent sans
        intermédiaire : c'est ce qui fait que `fj restore` reste interactif et
        que le code du CT devient celui de la commande.
        """
        extra = ("--yes",) if yes else ()
        self.runner.exec_replace(*self._argv(commande, args, *extra, env=env))


# ─── Confirmations, posées là où il y a un terminal ──────────────────────────


def confirm(question: str, attendu: str, quoi: str, *, saisie=None) -> None:
    """Exige la frappe exacte de `attendu`. Toute autre réponse annule.

    Pas de « oui/non » : recopier le nom oblige à le lire, et c'est exactement
    ce qu'on veut d'une commande qui écrase la base de la source de vérité.

    La fonction de saisie est résolue à l'APPEL et non liée par défaut : une
    valeur par défaut capturerait `input` au moment de l'import, et toute
    substitution ultérieure — un banc d'essai, un enrobage — serait ignorée.
    """
    lire = saisie if saisie is not None else input
    try:
        reponse = lire(f"{question} [tapez {quoi} pour confirmer] : ")
    except (EOFError, KeyboardInterrupt):
        raise Refus("annulé") from None
    if reponse != attendu:
        raise Refus("annulé")
