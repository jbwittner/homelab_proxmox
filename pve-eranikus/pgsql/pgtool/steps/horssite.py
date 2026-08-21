"""Section F — la copie hors-site, et la décision de l'armer.

Copier trois fichiers est la partie facile. Le cœur de cette section est
ailleurs : **faut-il armer le timer ?** La réponse dépend de trois constats
faits par d'autres étapes, et c'est là que le bash se trompait.

LE TROU DU BASH. `MP2_STATE` valait « inconnu » tant que la section A n'avait
pas tourné — ce qui arrive avec `--no-container`. La garde comparait
`== divergent`, ce qui est faux pour « inconnu » : le timer était donc armé
sans que le volume ait jamais été vérifié, et la copie de 3h30 partait sur une
source dont personne n'avait constaté la validité.

Ici, **un fait absent vaut « non déterminé » et bloque l'armement**. Ne pas
savoir n'est pas la même chose qu'aller bien, et un automatisme dont les
prérequis ne sont pas établis ne s'arme pas : on pose les fichiers, on laisse
le timer inactif, et on le dit. Un timer qui échoue toutes les nuits à 3h30
n'aide personne.

LA SOURCE SE DEMANDE, ELLE NE SE DEVINE PAS. Le chemin hôte du volume de
sauvegarde vient de `pvesm path`, pas d'une formule. Et il est vérifié : s'il
ne porte pas `subvol-<CTID>-`, il désigne le volume d'un AUTRE conteneur, et
la copie partirait sur les sauvegardes de quelqu'un d'autre — dans un bucket
qu'on ne peut pas purger.
"""

from __future__ import annotations

import stat
from pathlib import Path

from core.commands import Systemd
from core.converge import Action, Outcome
from core.log import CONT
from core.runner import CommandError
from proxmox import Container, Storage

# Ce que la clé du compte de service doit être. Elle n'est jamais créée par ce
# code : c'est un secret, il vient du gestionnaire de secrets.
MODE_CLE = 0o600

TIMER = "pgbk-offsite.timer"
EFFET_RELOAD = "host.daemon-reload"


class EtapeHorsSite:
    """Socle : sautée quand la copie hors-site n'est pas demandée."""

    section = "F"
    requires: tuple[str, ...] = ()

    def skip_if(self, ctx) -> str | None:
        if not ctx.opts.do_offsite:
            return "--no-offsite"
        return None


# ─── la source ───────────────────────────────────────────────────────────────


class SourceHorsSite(EtapeHorsSite):
    """Le chemin HÔTE du volume de sauvegarde, demandé à Proxmox.

    Le conteneur l'appelle `/var/backups/postgresql`, le nœud le voit ailleurs.
    C'est la confusion la plus facile à faire ici, et la raison pour laquelle
    ce chemin est demandé plutôt que recopié de mémoire.
    """

    name = "source hors-site"

    def check(self, ctx) -> Outcome:
        conteneur = Container(ctx.runner, ctx.opts.ctid)
        spec = conteneur.config().get("mp2", "")
        if not spec:
            return Outcome(
                "error",
                f"le CT {ctx.opts.ctid} n'a pas de mp2 — "
                "aucun volume de sauvegarde à copier",
            )
        # `data:subvol-200-disk-0,mp=…` : le volid s'arrête à la virgule, et il
        # contient lui-même un deux-points.
        volid = spec.split(",")[0]

        try:
            chemin = Storage(ctx.runner).path(volid)
        except CommandError:
            chemin = ""
        if not chemin:
            return Outcome("error", f"volume non résolu par pvesm : {volid}")

        # Le garde-fou : viser le volume d'un autre conteneur enverrait ses
        # sauvegardes dans notre bucket, et rien ne pourrait les en retirer.
        marque = f"subvol-{ctx.opts.ctid}-"
        if marque not in chemin:
            return Outcome(
                "error",
                f"{chemin} ne porte pas « {marque} » — "
                f"ce volume n'appartient pas au CT {ctx.opts.ctid}",
            )

        ctx.facts["offsite_src"] = chemin
        return Outcome("ok", f"{chemin} (vue hôte de /var/backups/postgresql)")


# ─── la clé ──────────────────────────────────────────────────────────────────


class CleGCP(EtapeHorsSite):
    """La clé du compte de service. Jamais créée, seulement constatée.

    C'est un secret : il n'a rien à faire dans le dépôt, et ce code ne peut pas
    le fabriquer. Il dit où le déposer et s'arrête là. Le seul défaut qu'il
    sache corriger est un mode trop ouvert.
    """

    name = "clé GCP"
    section = "E"

    def __init__(self, chemin: Path) -> None:
        self.chemin = Path(chemin)

    def check(self, ctx) -> Outcome:
        if not (self.chemin.is_file() and self.chemin.stat().st_size > 0):
            ctx.facts["gcp_key_ok"] = False
            return Outcome(
                "error",
                f"absente ou vide : {self.chemin}\n"
                f"{CONT}c'est un secret, hors dépôt par construction — "
                "la reposer depuis OpenBao",
            )

        mode = stat.S_IMODE(self.chemin.stat().st_mode)
        if mode != MODE_CLE:
            ctx.facts["gcp_key_ok"] = True
            return Outcome(
                "drift",
                f"{self.chemin} est en {mode:o}, attendu {MODE_CLE:o}",
                (
                    Action(
                        f"chmod {MODE_CLE:o} {self.chemin}",
                        lambda c, p=self.chemin: p.chmod(MODE_CLE),
                    ),
                ),
            )

        ctx.facts["gcp_key_ok"] = True
        return Outcome("ok", str(self.chemin))


# ─── les fichiers de l'hôte ──────────────────────────────────────────────────


class UniteHorsSite(EtapeHorsSite):
    """Un fichier du hors-site posé sur le nœud, script ou unité.

    Chaque pose DÉCLARE qu'un rechargement de systemd sera nécessaire ; le
    parcours n'en jouera qu'un, quel que soit le nombre de fichiers touchés.
    Le bash levait pour cela un drapeau `copied` à la main, à chaque appel.
    """

    def __init__(self, nom: str, cible: Path, *, mode: int = 0o644) -> None:
        self.name = nom
        self.cible = Path(cible)
        self.mode = mode

    def check(self, ctx) -> Outcome:
        source = ctx.paths.host_src / self.name
        if not source.is_file():
            return Outcome("error", f"absent du dépôt : {source}")

        conforme = (
            self.cible.is_file()
            and self.cible.read_bytes() == source.read_bytes()
            and stat.S_IMODE(self.cible.stat().st_mode) == self.mode
        )
        if conforme:
            return Outcome("ok", str(self.cible))

        etat = "drift" if self.cible.exists() else "absent"
        return Outcome(
            etat,
            str(self.cible),
            (
                Action(
                    f"install -m {self.mode:o} {source} {self.cible}",
                    lambda c, s=source, d=self.cible, m=self.mode:
                        c.fs.install(s, d, mode=m),
                    effects=frozenset({EFFET_RELOAD}),
                ),
            ),
        )


# ─── l'armement ──────────────────────────────────────────────────────────────


class ArmementHorsSite(EtapeHorsSite):
    """Armer le timer, ou dire pourquoi on ne le fait pas.

    Trois conditions, et **aucune supposition** : le volume de sauvegarde doit
    être conforme, la clé présente, `rclone` installé. Un fait non établi
    compte comme un refus, pas comme un feu vert.
    """

    name = "pgbk-offsite.timer (armement)"

    def _manquants(self, ctx) -> list[str]:
        raisons: list[str] = []

        mp2 = ctx.facts.get("mp2_state")
        if mp2 is None:
            raisons.append(
                "l'état de mp2 n'a pas été établi (--no-container ?) — "
                "on n'arme pas sur une supposition"
            )
        elif mp2 != "ok":
            raisons.append(f"mp2 est {mp2}")

        if not ctx.facts.get("gcp_key_ok"):
            raisons.append("la clé du compte de service manque")
        if not ctx.facts.get("rclone_ok", True):
            raisons.append("rclone n'est pas installé")
        if not ctx.facts.get("offsite_src"):
            raisons.append("la source hors-site n'est pas résolue")
        return raisons

    def check(self, ctx) -> Outcome:
        arme = Systemd(ctx.runner).is_enabled(TIMER)
        manquants = self._manquants(ctx)

        if manquants:
            motif = " ; ".join(manquants)
            if arme:
                # Le cas vicieux : il tourne, mais un prérequis a disparu.
                # Personne ne regarde à 3h30.
                return Outcome(
                    "error",
                    f"actif mais prérequis manquants — la copie échouera "
                    f"à 3h30 : {motif}",
                )
            return Outcome(
                "error",
                f"non armé, et c'est voulu : {motif}",
            )

        if arme:
            return Outcome("ok", "actif")
        return Outcome(
            "absent",
            "prérequis réunis, à armer",
            (
                Action(
                    f"systemctl enable --now {TIMER}",
                    lambda c: Systemd(c.runner).enable_now(TIMER),
                ),
            ),
        )
