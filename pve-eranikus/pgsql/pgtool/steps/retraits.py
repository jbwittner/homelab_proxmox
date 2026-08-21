"""Section H — retirer ce que plus rien n'appelle.

**Supprimer un script du dépôt ne le retire pas du nœud.** Le binaire installé
y reste, exécutable, périmé, et quelqu'un le rejouera dans un an en croyant
faire le bon geste. Une bascule franche demande donc une étape de retrait
explicite : c'est la seule chose qui distingue « remplacé » de « doublé ».

**Un retrait est conditionnel à ce qui l'a remplacé.** Retirer le script
hors-site alors que l'unité qui le remplace n'est pas conforme laisserait le
nœud sans aucune copie, et le timer échouerait chaque nuit à 3h30 sans que
personne ne fasse le lien. Le prérequis n'est pas une précaution de style :
c'est ce qui distingue un retrait d'une régression.

**Le motif nomme le remplaçant.** Un retrait qui ne dit pas par quoi se lit
comme une perte de fonction, et ne se relit pas dans six mois.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from core.converge import Action, Outcome


class RetraitOrphelin:
    """Un fichier installé que plus rien n'appelle."""

    section = "H"

    def __init__(
        self,
        chemin: Path,
        *,
        remplace_par: str,
        requires: Sequence[str] = (),
    ) -> None:
        self.chemin = Path(chemin)
        self.remplace_par = remplace_par
        self.requires = tuple(requires)
        self.name = f"retrait de {self.chemin.name}"

    def skip_if(self, ctx) -> str | None:
        return None

    def check(self, ctx) -> Outcome:
        if not self.chemin.exists():
            # Une fois fait, un retrait ne se redit pas : « zéro modification
            # sur un état conforme » vaut aussi pour ce qui n'est plus là.
            return Outcome("ok", f"déjà retiré, remplacé par {self.remplace_par}")
        return Outcome(
            "drift",
            f"{self.chemin} est périmé, remplacé par {self.remplace_par}",
            (
                Action(
                    f"rm {self.chemin}",
                    lambda c, p=self.chemin: c.fs.remove(p),
                ),
            ),
        )
