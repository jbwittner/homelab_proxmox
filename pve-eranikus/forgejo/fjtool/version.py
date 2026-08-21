"""L'épinglage de version : le lire, le résoudre, le vérifier.

C'est le module qui porte la raison d'être de tout ce service. Le script
communautaire `ct/forgejo.sh` appelle `fetch_and_deploy_codeberg_release …
"latest" …` : la version y est en dur, il ne peut installer QUE la dernière
publication, et sa fonction de mise à jour redéploie `latest` sans prompt ni
sauvegarde. Sur une branche non-LTS, cela fait sauter une majeure — avec une
migration de schéma irréversible — un matin, sans que personne l'ait demandé.

TROIS RÈGLES, ET ELLES SE TIENNENT.

**La version vit dans un fichier du dépôt**, pas dans un argument tapé une
fois. Un épinglage qui n'est pas traçable n'est pas un épinglage : on ne peut
ni le relire, ni le comparer à ce qui tourne, ni voir dans `git log` quand il
a bougé et pourquoi.

**La résolution est séparée de la pose.** `fj version --resolve` interroge
Codeberg et réécrit `VERSION` ; `fj deploy` n'interroge rien et pose
exactement ce que `VERSION` dit. Un déploiement ne doit jamais dépendre de ce
qu'un serveur distant répond ce jour-là — c'est précisément le défaut du
script communautaire.

**Rien ne s'installe sans être vérifié.** Somme de contrôle ET signature. La
somme seule ne prouve rien : elle voyage sur le même canal que le binaire.
C'est la signature qui rattache l'artefact à une clé, et cette clé doit avoir
été obtenue AUTREMENT que par le canal qu'elle sert à valider — d'où
`ct/RELEASE-KEY.asc`, déposé à la main une fois. Voir doc/RUNBOOK.md § 4.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# La branche épinglée. Changer ces deux valeurs est une DÉCISION, prise en
# lisant les notes de version : une migration de schéma Forgejo ne se rejoue
# pas à l'envers. Elles sont ici, et à un seul endroit, pour que ce soit un
# changement visible dans une revue.
BRANCHE = "15.0"
EOL = "15 juillet 2027"

API = "https://codeberg.org/api/v1/repos/forgejo/forgejo/releases"
TELECHARGEMENT = "https://codeberg.org/forgejo/forgejo/releases/download"

# Un délai court : la résolution est un geste interactif, pas un automatisme.
# Un opérateur qui attend doit voir un échec, pas un curseur.
TIMEOUT = 30

_VERSION = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class VersionError(RuntimeError):
    """Épinglage inutilisable. Toujours dit avec ce qu'il faut taper ensuite."""


@dataclass(frozen=True)
class Release:
    """Une publication retenue, réduite à ce qui nous intéresse."""

    tag: str

    @property
    def nu(self) -> str:
        """Le numéro sans le « v » — c'est cette forme qui nomme l'artefact."""
        return self.tag[1:]

    @property
    def binaire(self) -> str:
        return f"forgejo-{self.nu}-linux-amd64"

    def url(self, suffixe: str = "") -> str:
        return f"{TELECHARGEMENT}/{self.tag}/{self.binaire}{suffixe}"


# ─── Lecture du fichier VERSION ──────────────────────────────────────────────


def parse(texte: str) -> str | None:
    """La première ligne utile d'un fichier VERSION, ou None.

    Tout ce qui commence par « # » est un commentaire, les lignes vides sont
    ignorées. `None` veut dire « non résolue » et JAMAIS « la dernière » : la
    différence est exactement celle que ce service existe pour maintenir.
    """
    for ligne in texte.splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#"):
            continue
        return ligne
    return None


def lire(chemin: Path) -> str | None:
    if not chemin.is_file():
        return None
    return parse(chemin.read_text(encoding="utf-8"))


def valider(version: str, *, branche: str = BRANCHE) -> str:
    """Refuse tout ce qui n'est pas « vX.Y.Z » sur la branche épinglée.

    Le contrôle de branche n'est pas de la pédanterie : c'est le seul endroit
    où une 16.0 collée à la main dans VERSION est arrêtée. Sans lui, le fichier
    ne documenterait l'épinglage qu'aux gens déjà au courant.
    """
    if not _VERSION.fullmatch(version):
        raise VersionError(
            f"version mal formée : « {version} » — attendu vX.Y.Z"
        )
    if not version.startswith(f"v{branche}."):
        raise VersionError(
            f"« {version} » n'est pas sur la branche LTS {branche} — "
            f"changer de branche est une décision, pas un correctif : "
            f"voir doc/RUNBOOK.md section 4"
        )
    return version


def cle_de_tri(tag: str) -> tuple[int, int, int]:
    """Trie NUMÉRIQUEMENT. « v15.0.10 » est plus récent que « v15.0.9 », ce
    qu'un tri lexicographique conclurait à l'envers — et ce jour-là on
    installerait une version plus ancienne en croyant faire l'inverse."""
    m = _VERSION.fullmatch(tag)
    if not m:
        raise VersionError(f"tag mal formé : {tag}")
    return (int(m[1]), int(m[2]), int(m[3]))


def rendre(version: str, *, branche: str = BRANCHE, eol: str = EOL) -> str:
    """Le contenu du fichier VERSION, en-tête compris.

    Réécrire le fichier entier plutôt que d'y substituer une ligne : c'est ce
    qui garantit que l'avertissement « ce CT n'est jamais mis à jour par un
    script communautaire » ne peut pas se perdre au fil des résolutions.
    """
    return (
        "# Version de Forgejo ÉPINGLÉE pour le CT 400.\n"
        "#\n"
        "# Une seule ligne utile : « vMAJEUR.MINEUR.CORRECTIF ». Tout ce qui\n"
        "# commence par « # » est un commentaire, les lignes vides sont ignorées.\n"
        "#\n"
        "# CE CONTENEUR N'EST JAMAIS MIS À JOUR PAR UN SCRIPT COMMUNAUTAIRE.\n"
        f"# La branche {branche} est LTS — fin de support : {eol}.\n"
        "# En sortir est une DÉCISION, prise après lecture des notes de version :\n"
        "# une migration de schéma Forgejo est irréversible sans restauration.\n"
        "# Voir doc/RUNBOOK.md section 4.\n"
        "#\n"
        "# Écrit par « fj version --resolve ». Résoudre n'installe rien : c'est\n"
        "# « fj deploy » qui pose, et il ne pose QUE ce qui est écrit ci-dessous.\n"
        "\n"
        f"{version}\n"
    )


# ─── Résolution depuis Codeberg ──────────────────────────────────────────────


def retenir(publications: Iterable[dict], *, branche: str = BRANCHE) -> Release:
    """La plus récente publication stable de la branche.

    Fonction PURE : toute la décision est ici, et se teste sans réseau. Ce qui
    est écarté, et pourquoi :

      - `draft` — une publication pas encore annoncée, dont les artefacts
        peuvent changer sous le même tag ;
      - `prerelease` — les `-rc`, qui portent parfois un tag propre en apparence ;
      - tout ce qui n'est pas sur la branche épinglée.
    """
    prefixe = f"v{branche}."
    candidats = [
        tag
        for p in publications
        if not p.get("draft") and not p.get("prerelease")
        if (tag := str(p.get("tag_name") or "")).startswith(prefixe)
        if _VERSION.fullmatch(tag)
    ]
    if not candidats:
        raise VersionError(
            f"aucune publication stable sur la branche {branche} — "
            f"la branche est-elle encore maintenue ? (fin de support : {EOL})"
        )
    return Release(max(candidats, key=cle_de_tri))


def interroger(url: str = API, *, timeout: int = TIMEOUT) -> list[dict]:
    """L'appel réseau, isolé pour que `retenir` reste testable sans lui.

    Sort en `VersionError` sur tout incident : une résolution qui échoue doit
    dire quoi faire, pas remonter une trace d'urllib.
    """
    requete = urllib.request.Request(
        f"{url}?limit=50", headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(requete, timeout=timeout) as reponse:
            charge = reponse.read()
    except Exception as exc:  # noqa: BLE001 - toute panne réseau, même cause
        raise VersionError(
            f"Codeberg injoignable ({type(exc).__name__}: {exc}) — "
            "la résolution demande un accès sortant depuis le nœud ; "
            "à défaut, écrire la version à la main dans ct/VERSION"
        ) from exc
    try:
        publications = json.loads(charge)
    except json.JSONDecodeError as exc:
        raise VersionError(f"réponse Codeberg illisible : {exc}") from exc
    if not isinstance(publications, list):
        raise VersionError("réponse Codeberg inattendue : une liste était attendue")
    return publications


def resoudre(*, branche: str = BRANCHE, url: str = API) -> Release:
    return retenir(interroger(url), branche=branche)


# ─── Ce que le binaire posé déclare de lui-même ──────────────────────────────


def version_installee(sortie: str) -> str | None:
    """Extrait « v15.0.3 » de la sortie de `forgejo --version`.

    La ligne ressemble à « Forgejo version 15.0.3+gitea-1.22.0 built with … ».
    On ne compare donc JAMAIS la chaîne entière : le suffixe de compatibilité
    Gitea change sans que la version de Forgejo bouge, et une comparaison
    littérale annoncerait une dérive à chaque déploiement.
    """
    m = re.search(r"\bversion\s+v?(\d+\.\d+\.\d+)", sortie)
    return f"v{m[1]}" if m else None
