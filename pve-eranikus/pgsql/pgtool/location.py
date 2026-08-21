"""Où tourne-t-on, et à qui déléguer.

Le même exécutable sert des deux côtés du montage : sur le nœud il achemine,
dans le conteneur il travaille. Le bash décidait en tête de script, sur la
présence de `pct` ; ici c'est isolé dans ce module, parce que cette décision
gouverne tout le reste et mérite d'être lisible et testable à part.

DEUX ASYMÉTRIES GOUVERNENT CE FICHIER.

`pct exec` n'alloue pas de TTY. Une question posée depuis le conteneur ne
verrait jamais la saisie : la confirmation destructive se pose donc **côté
hôte**, là où le terminal existe, et le conteneur reçoit `--yes`. Ce n'est pas
un raffinement d'ergonomie — sans cela, la garde de `restore` serait muette.

Le conteneur seul sait à quoi une référence correspond. « 20260819 » désigne
la plus récente de ce jour-là, qui peut être le dernier instantané, celui que
le moteur protège. La question doit donc porter sur ce qui sera **réellement**
supprimé, pas sur ce qui a été tapé : d'où `--plan`, qui applique toutes les
gardes, n'efface rien, et n'écrit sur la sortie standard que le nom résolu.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

from core.log import error
from core.runner import CommandError, Runner

# Chemin du moteur DANS le conteneur. Absolu : le PATH de `pct exec` est
# minimal et n'inclut pas /usr/local/bin.
CT_PGBK = "/usr/local/bin/pgbk"

# Le CTID est consigné à un seul endroit, écrit par `pg deploy`.
CONF = Path("/etc/default/pgbk")


class Where(Enum):
    HOST = "nœud"
    CONTAINER = "conteneur"


class Refus(RuntimeError):
    """Refus argumenté : un message, et un code de retour 1.

    Le bash appelait ça `die`. Le nom change, pas la sémantique.
    """


def detect(runner: Runner) -> Where:
    """Sur la présence de `pct` — un nœud Proxmox l'a, un conteneur Debian non.

    C'est le même critère que le bash. Le changer casserait la cohabitation
    des deux implémentations pendant la migration.
    """
    return Where.HOST if runner.which("pct") else Where.CONTAINER


def read_conf(chemin: Path = CONF) -> dict[str, str]:
    """Lit un fichier `CLÉ=valeur`, sans shell.

    Le bash faisait un `source`, qui exécute. Ici on analyse : un fichier de
    configuration n'a pas à pouvoir lancer des commandes.
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
    *, flag: str | None, env: Mapping[str, str], conf: Mapping[str, str]
) -> int:
    """`--ctid`, puis l'environnement, puis le fichier. Jamais de défaut.

    `pg deploy` garde un défaut parce qu'il doit pouvoir amorcer une
    installation vierge. Ici, non : deviner un CTID, c'est risquer de
    restaurer une base dans le mauvais conteneur.
    """
    brut = flag or env.get("PG_CTID") or conf.get("PG_CTID") or ""
    if not brut:
        raise Refus(
            f"aucun conteneur cible : {CONF} absent ou sans PG_CTID\n"
            "         le consigner  : pg deploy --ctid <ID>\n"
            "         ou ponctuel   : pg --ctid <ID> <commande>"
        )
    if not re.fullmatch(r"[0-9]+", brut):
        raise Refus(f"CTID invalide : {brut}")
    return int(brut)


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
            "pct", "exec", str(self.ctid), "--", "test", "-x", CT_PGBK
        ):
            raise Refus(
                f"{CT_PGBK} absent du CT {self.ctid} — le poser : pg deploy"
            )

    def _argv(self, commande: str, args: Sequence[str], *extra: str) -> list[str]:
        return [
            "pct", "exec", str(self.ctid), "--", CT_PGBK, commande, *args, *extra
        ]

    def plan(self, commande: str, args: Sequence[str]) -> str:
        """Le nom réellement visé, sans rien effacer.

        Contrat du moteur : la sortie standard ne porte QUE ce nom ; le détail
        humain part sur la sortie d'erreur. Un code non nul veut dire « refusé
        » — une garde a parlé — et son message est déjà passé à l'écran.
        """
        try:
            res = self.runner.read(*self._argv(commande, args, "--plan"))
        except CommandError as exc:
            for ligne in exc.result.stderr.splitlines():
                error(ligne)
            raise Refus("") from exc
        return res.out

    def hand_over(self, commande: str, args: Sequence[str], *, yes: bool) -> None:
        """Remplace ce processus par la commande du conteneur.

        Le terminal, l'entrée standard et le code de retour passent sans
        intermédiaire : c'est ce qui fait que `pg restore` reste interactif et
        que le code du CT devient celui de la commande. Une capture par tuyau
        les perdrait tous les trois.
        """
        extra = ("--yes",) if yes else ()
        self.runner.exec_replace(*self._argv(commande, args, *extra))


# ─── Confirmations, posées là où il y a un terminal ──────────────────────────


def confirm(question: str, attendu: str, quoi: str, *, saisie=None) -> None:
    """Exige la frappe exacte de `attendu`. Toute autre réponse annule.

    Pas de « oui/non » : recopier le nom oblige à le lire, et c'est
    exactement ce qu'on veut d'une commande qui écrase une base ou supprime un
    instantané.

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


def first_positional(args: Sequence[str]) -> str | None:
    """Le premier argument qui n'est pas une option."""
    for a in args:
        if not a.startswith("--"):
            return a
    return None
