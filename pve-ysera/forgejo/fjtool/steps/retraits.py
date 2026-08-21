"""Section H — ce qui ne doit PAS être là.

Deux natures de constat, et la seconde est la raison d'être de tout ce
service.

**Un fichier périmé sur le nœud.** Supprimer un script du dépôt ne le retire
pas de la machine : le binaire installé y reste, exécutable, et quelqu'un le
rejouera dans un an en croyant faire le bon geste. Un retrait est conditionnel
à ce qui l'a remplacé, et son motif NOMME le remplaçant — sinon il se lit comme
une perte de fonction.

**Un automatisme de mise à jour dans le conteneur.** Celui-là ne se retire pas
par précaution de rangement : il se retire parce qu'il détruirait la source de
vérité. La fonction `update_script()` du script communautaire redéploie
`latest` sans question et sans sauvegarde préalable — sur une branche non-LTS,
elle fait sauter une majeure en octobre, avec une migration de schéma que rien
ne rejoue à l'envers. Tout ce qui pourrait la déclencher est cherché, et
signalé comme une **erreur**, jamais corrigé en silence : un automatisme qu'on
n'a pas posé soi-même est une trace de quelque chose qu'il faut comprendre
avant d'effacer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from core.converge import Action, Outcome


class RetraitOrphelin:
    """Un fichier installé sur le NŒUD que plus rien n'appelle."""

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


class AucunAutoUpdate:
    """Rien, dans le conteneur, ne doit pouvoir mettre Forgejo à jour tout seul.

    Ce contrôle n'a pas d'action, et c'est délibéré. Trouver ici un timer ou un
    script qu'on n'a pas posé veut dire que quelqu'un — un script communautaire
    rejoué, une bonne intention — a installé un chemin de mise à jour. Le
    supprimer sans comprendre d'où il vient laisserait la cause en place.

    Ce qui est cherché :

      - les unités systemd dont le nom évoque une mise à jour de Forgejo ;
      - les entrées cron ou les timers `apt` qui viseraient le binaire ;
      - le script `update` que le paquet communautaire dépose dans le PATH.

    Un `unattended-upgrades` générique n'est PAS concerné : Forgejo n'est pas
    un paquet Debian ici, il ne peut pas être emporté par une mise à jour de
    paquets. C'est précisément l'un des intérêts de l'installation binaire.
    """

    name = "aucun automatisme de mise à jour"
    section = "H"
    requires: tuple[str, ...] = ()

    # Chemins que le script communautaire ou un successeur déposerait.
    SUSPECTS = (
        "/usr/bin/update",
        "/usr/local/bin/update",
        "/etc/systemd/system/forgejo-update.service",
        "/etc/systemd/system/forgejo-update.timer",
        "/etc/cron.daily/forgejo",
        "/etc/cron.d/forgejo",
    )

    def skip_if(self, ctx) -> str | None:
        return None

    def check(self, ctx) -> Outcome:
        ct = ctx.runner.for_container(ctx.opts.ctid)
        # Un seul aller-retour pour toute la liste : le script est CONSTANT et
        # les chemins arrivent en arguments positionnels.
        res = ct.read(
            "sh", "-c",
            'for f in "$@"; do [ -e "$f" ] && echo "$f"; done; true',
            "sh", *self.SUSPECTS,
            check=False,
        )
        trouves = res.lines
        if not trouves:
            return Outcome(
                "ok", f"{len(self.SUSPECTS)} chemin(s) vérifié(s), aucun présent"
            )
        return Outcome(
            "error",
            "chemin(s) de mise à jour automatique présent(s) : "
            + ", ".join(trouves)
            + " — un redéploiement en « latest » ferait sauter une majeure "
            "avec migration de schéma irréversible. Comprendre d'où ils "
            "viennent AVANT de les retirer (doc/RUNBOOK.md section 4)",
        )
