"""Le modèle d'instantané : une seule implémentation, pour trois qui divergeaient.

Le bash comptait l'âge de deux façons incompatibles — en jours calendaires dans
`pgbk list`, en périodes de 24 h tronquées dans `prune` — et résolvait les
références à trois endroits. C'est ici, une fois.

CE QUI FAIT QU'UN INSTANTANÉ EST COMPLET. `pg-backup.sh` écrit tout dans
`<horodatage>.part/`, puis renomme. Le renommage est le point de non-retour :
**un répertoire sans le suffixe est complet par construction**. Il n'y a pas de
fichier témoin, et il n'en faut pas — c'est l'atomicité du renommage qui porte
la garantie.

L'ÂGE PORTE SUR LE mtime, PAS SUR LE NOM. Le renommage conserve le mtime, qui
vaut donc l'instant d'achèvement. Lire le nom donnerait un autre résultat sur un
instantané recopié, restauré depuis GCS, ou simplement touché après coup.

LA RÉTENTION EST CELLE DE `find -mtime`, et ce n'est pas la même chose que des
jours calendaires : `-mtime +14` supprime à partir de **15 × 24 h**, parce que
`find` tronque l'âge en périodes de 24 h et compare strictement. Une
implémentation « âge > 14 jours » purgerait un jour trop tôt. Ce module PRÉDIT
ce que le bash purgera ; c'est `pg-backup.sh` qui purge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

JOUR = 86400

# Un instantané complet : « 20… » sans suffixe. Les deux autres formes sont
# exclues partout, et chacune pour sa raison.
PREFIXE = "20"
SUFFIXE_PART = ".part"
PREFIXE_FILET = "pre-restore-"

_STAMP = re.compile(r"^\d{8}-\d{6}$")
_DAY = re.compile(r"^\d{8}$")


class Reference(Enum):
    """Les trois formes acceptées, et le refus."""

    LATEST = "latest"
    STAMP = "AAAAMMJJ-HHMMSS"
    DAY = "AAAAMMJJ"
    INVALID = "incomprise"


def parse_reference(ref: str) -> Reference:
    """Classe une référence SANS toucher au disque.

    Le bash utilisait des motifs glob (`[0-9]*-[0-9]*`), qui acceptaient
    « 20260820-093240.part » et n'importe quelle longueur. Ici la forme est
    exacte : un instantané incomplet n'est pas restaurable, le dire à l'analyse
    plutôt que trois gardes plus loin.
    """
    if ref == "latest":
        return Reference.LATEST
    if _STAMP.fullmatch(ref):
        return Reference.STAMP
    if _DAY.fullmatch(ref):
        return Reference.DAY
    return Reference.INVALID


def age_jours(*, mtime: float, maintenant: float) -> int:
    """Périodes de 24 h écoulées, tronquées — la sémantique de `find -mtime`."""
    return int((maintenant - mtime) // JOUR)


def expires(*, mtime: float, maintenant: float, retention: int) -> bool:
    """Vrai si `find -mtime +<retention>` sélectionnerait ce répertoire.

    Comparaison STRICTE sur l'âge tronqué : avec 14, la suppression commence à
    15 × 24 h. L'écrire ainsi plutôt qu'en jours évite aussi le décalage d'un
    cran au changement d'heure, l'epoch ignorant les heures d'été.
    """
    return age_jours(mtime=mtime, maintenant=maintenant) > retention


@dataclass(frozen=True)
class Snapshot:
    path: Path

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def complete(self) -> bool:
        return not self.name.endswith(SUFFIXE_PART)

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(sorted(p.name for p in self.path.iterdir() if p.is_file()))

    @property
    def databases(self) -> tuple[str, ...]:
        """Une base par `<nom>.dump`. Le MANIFEST les liste aussi, mais les
        fichiers font foi : c'est ce qui sera restauré."""
        return tuple(f[:-5] for f in self.files if f.endswith(".dump"))

    @property
    def has_manifest(self) -> bool:
        return "MANIFEST" in self.files

    @property
    def has_globals(self) -> bool:
        """Sans `globals.sql`, les rôles manquent — et `restore` refusera une
        base dont le propriétaire n'existe pas. Ce refus est le rappel."""
        return "globals.sql" in self.files

    def dump(self, database: str) -> Path:
        return self.path / f"{database}.dump"

    def mtime(self) -> float:
        return self.path.stat().st_mtime

    def age_days(self, *, maintenant: float) -> int:
        return age_jours(mtime=self.mtime(), maintenant=maintenant)

    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.path.rglob("*") if p.is_file())

    def manifest(self) -> dict[str, str]:
        """« clé : valeur » alignées par `pg-backup.sh`. Découpe sur le PREMIER
        deux-points : la date ISO en contient."""
        chemin = self.path / "MANIFEST"
        if not chemin.is_file():
            return {}
        entrees: dict[str, str] = {}
        for ligne in chemin.read_text(encoding="utf-8").splitlines():
            cle, sep, valeur = ligne.partition(":")
            if sep:
                entrees[cle.strip()] = valeur.strip()
        return entrees


class Store:
    """Le répertoire des sauvegardes, vu comme une collection d'instantanés."""

    def __init__(self, dest: Path) -> None:
        self.dest = Path(dest)

    # -- inventaire ---------------------------------------------------------

    def _entries(self) -> list[Path]:
        if not self.dest.is_dir():
            return []
        return [
            p for p in self.dest.iterdir()
            if p.is_dir() and not p.is_symlink() and p.name.startswith(PREFIXE)
        ]

    def snapshots(self) -> list[Snapshot]:
        """Les instantanés complets, dans l'ordre chronologique.

        L'horodatage fait coïncider ordre lexicographique et ordre
        chronologique : trier les noms suffit, et reste stable même si un
        mtime a été touché.
        """
        return sorted(
            (Snapshot(p) for p in self._entries()
             if not p.name.endswith(SUFFIXE_PART)),
            key=lambda s: s.name,
        )

    def debris(self, *, maintenant: float) -> list[Snapshot]:
        """Les `.part` de plus de 48 h. En dessous, c'est peut-être une
        sauvegarde en cours — `find -mtime +1` les épargne exprès."""
        return sorted(
            (Snapshot(p) for p in self._entries()
             if p.name.endswith(SUFFIXE_PART)
             and expires(mtime=p.stat().st_mtime, maintenant=maintenant,
                         retention=1)),
            key=lambda s: s.name,
        )

    def expired(self, *, retention: int, maintenant: float) -> list[Snapshot]:
        """Ce que `pg-backup.sh` purgera à sa prochaine exécution."""
        return [
            s for s in self.snapshots()
            if expires(mtime=s.mtime(), maintenant=maintenant,
                       retention=retention)
        ]

    def latest(self) -> Snapshot | None:
        """Le plus récent, celui que `delete` protège.

        Par le nom et non par le lien `latest` : le lien peut manquer, et sa
        disparition ne doit pas lever la protection.
        """
        instantanes = self.snapshots()
        return instantanes[-1] if instantanes else None

    # -- résolution ---------------------------------------------------------

    def resolve(self, ref: str) -> Snapshot:
        """Une référence → un instantané, ou une exception.

        `ValueError` si la référence n'a pas de sens, `LookupError` si elle en
        a un mais ne désigne rien. Les deux sont distinctes parce que le
        remède ne l'est pas : corriger sa frappe, ou constater une absence.
        """
        forme = parse_reference(ref)
        if forme is Reference.INVALID:
            raise ValueError(
                f"référence incomprise : {ref} "
                "(attendu: latest, AAAAMMJJ, ou AAAAMMJJ-HHMMSS)"
            )

        if forme is Reference.LATEST:
            lien = self.dest / "latest"
            if not lien.is_symlink():
                raise LookupError(f"aucune sauvegarde : {lien} absent")
            cible = lien.resolve()
        elif forme is Reference.DAY:
            candidats = [s for s in self.snapshots() if s.name.startswith(f"{ref}-")]
            if not candidats:
                raise LookupError(f"aucune sauvegarde pour le {ref}")
            cible = candidats[-1].path  # le plus récent de ce jour
        else:
            cible = self.dest / ref

        # Confinement. Le bash posait cette garde après coup, avec un
        # commentaire sur le risque d'un « rm -rf $PWD » ; ici elle est sur le
        # seul chemin de sortie, donc impossible à contourner.
        dest_reelle = self.dest.resolve()
        if cible.parent.resolve() != dest_reelle:
            raise LookupError(f"hors de {self.dest} : {cible} — refus")
        if not cible.is_dir():
            raise LookupError(f"instantané introuvable : {cible}")
        return Snapshot(cible)
