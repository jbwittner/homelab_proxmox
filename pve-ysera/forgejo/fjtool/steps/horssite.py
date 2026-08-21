"""Section F — la copie hors-site, et la décision de l'armer.

Copier des fichiers est la partie facile. Le cœur de cette section est
ailleurs : **faut-il armer le timer ?** La réponse dépend de constats faits par
d'autres étapes, et **un fait absent vaut « non déterminé »**, jamais « tout va
bien ». Ne pas savoir n'est pas la même chose qu'aller bien : un automatisme
dont les prérequis ne sont pas établis ne s'arme pas. On pose les fichiers, on
laisse le timer inactif, et on le dit — un timer qui échoue toutes les nuits à
3h50 n'aide personne.

Le CT 200 a payé ce défaut : sa garde comparait `MP2_STATE == divergent`, ce
qui est faux pour « inconnu », et le timer s'armait donc sans que le volume ait
jamais été vérifié.

LA SOURCE SE DEMANDE, ELLE NE SE DEVINE PAS. Le chemin hôte du volume de
sauvegarde vient de `pvesm path`, pas d'une formule. Et il est vérifié : s'il
ne porte pas `subvol-<CTID>-`, il désigne le volume d'un AUTRE conteneur, et
la copie partirait sur les sauvegardes de quelqu'un d'autre — dans un bucket
qu'on ne peut pas purger, puisque le compte de service n'a pas le droit de
supprimer.
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

TIMER = "fjbk-offsite.timer"
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

    Le conteneur l'appelle `/var/backups/forgejo`, le nœud le voit ailleurs.
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
        # `data:subvol-400-disk-0,mp=…` : le volid s'arrête à la virgule, et il
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
        return Outcome(
            "ok", f"{chemin} (vue hôte de {ctx.opts.mp2_mount})"
        )


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
    """Un fichier du hors-site posé sur le nœud.

    Chaque pose DÉCLARE qu'un rechargement de systemd sera nécessaire ; le
    parcours n'en jouera qu'un, quel que soit le nombre de fichiers touchés.
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

    Quatre conditions, et **aucune supposition** : le volume de sauvegarde doit
    être conforme, la clé présente, `rclone` installé, la source résolue. Un
    fait non établi compte comme un refus, pas comme un feu vert.
    """

    name = "fjbk-offsite.timer (armement)"

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
                # Personne ne regarde à 3h50.
                return Outcome(
                    "error",
                    f"actif mais prérequis manquants — la copie échouera "
                    f"à 3h50 : {motif}",
                )
            return Outcome("error", f"non armé, et c'est voulu : {motif}")

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


# ─── ce que l'unité du dépôt déclare ─────────────────────────────────────────


def unit_env(unite: Path, cle: str, defaut: str) -> str:
    """Valeur d'un `Environment=CLE=valeur` dans l'unité DU DÉPÔT.

    Le dépôt fait foi, ni l'unité installée ni le drop-in : c'est ce que le
    déploiement s'apprête à poser. Interroger `systemctl show` répondrait sur
    l'état d'avant — celui qu'on est justement en train de remplacer.

    La DERNIÈRE occurrence gagne, comme chez systemd. Et un défaut est rendu
    quand la clé manque : sans lui, une ligne retirée produirait un contrôle
    sur une chaîne vide, qui passerait pour un fichier absent.
    """
    valeur = defaut
    try:
        lignes = Path(unite).read_text().splitlines()
    except OSError:
        return defaut
    for ligne in lignes:
        nom, _, reste = ligne.strip().partition("=")
        if nom != "Environment":
            continue
        # Découpe sur le PREMIER « = » seulement : une valeur qui en
        # contiendrait un ne doit pas être tronquée.
        trouvee, _, contenu = reste.partition("=")
        if trouvee == cle:
            valeur = contenu
    return valeur


# ─── rclone.conf ─────────────────────────────────────────────────────────────

LIGNE_UBLA = "bucket_policy_only = true"


class ConfigRclone(EtapeHorsSite):
    """Le remote de la copie. Écrit s'il est ABSENT, jamais réécrit.

    Ce fichier est hors dépôt et peut porter d'autres remotes que le nôtre : le
    réécrire en emporterait. Quand il existe mais qu'il lui manque
    `bucket_policy_only`, la ligne exacte est DICTÉE et rien n'est ajouté —
    éditer un fichier qu'on ne possède pas est un geste qui appartient à
    l'humain.

    Sans cette ligne, l'accès uniforme du bucket (UBLA) refuse chaque insertion
    en « Error 400: Cannot insert legacy ACL for an object when uniform
    bucket-level access is enabled », zéro octet écrit. Constaté sur
    pve-eranikus le 20 août 2026.
    """

    name = "rclone.conf"

    def __init__(self, chemin: Path, *, remote: str, cle: Path) -> None:
        self.chemin = Path(chemin)
        self.remote = remote
        self.cle = Path(cle)

    def _contenu(self) -> str:
        return "\n".join((
            "# Généré par fj deploy — remote de la copie hors-site.",
            f"[{self.remote}]",
            "type = google cloud storage",
            f"service_account_file = {self.cle}",
            LIGNE_UBLA,
        )) + "\n"

    def check(self, ctx) -> Outcome:
        if not self.chemin.is_file():
            return Outcome(
                "absent",
                f"{self.chemin} — remote [{self.remote}] à écrire",
                (
                    Action(
                        f"écrire {self.chemin} (0600)",
                        lambda c, p=self.chemin, t=self._contenu():
                            c.fs.write_file(p, t, mode=0o600),
                    ),
                ),
            )

        texte = self.chemin.read_text()
        if not any(
            ligne.strip().startswith("bucket_policy_only")
            for ligne in texte.splitlines()
        ):
            return Outcome(
                "error",
                f"{LIGNE_UBLA} absent de {self.chemin} — sans lui, UBLA "
                f"refuse chaque insertion en 400 (doc/RUNBOOK.md section 10)\n"
                f"{CONT}echo '{LIGNE_UBLA}' >> {self.chemin}",
            )
        return Outcome("ok", str(self.chemin))


# ─── le drop-in du nœud ──────────────────────────────────────────────────────


class DropInNoeud(EtapeHorsSite):
    """CE nœud-ci, CE volume-ci. Le drop-in fait autorité.

    L'unité du dépôt ne porte que des valeurs par défaut lisibles ; c'est ce
    fichier qui rend le hors-site juste quel que soit le CTID, sans rien éditer
    dans le dépôt.

    Il n'est PAS écrit tant que la source n'a pas été résolue : y inscrire un
    chemin deviné ferait partir la copie de 3h50 sur un volume inventé.
    """

    name = "drop-in du nœud"
    requires = ("source hors-site",)

    def __init__(self, chemin: Path, *, node: str) -> None:
        self.chemin = Path(chemin)
        self.node = node

    def _contenu(self, source: str) -> str:
        return "\n".join((
            "# Généré par fj deploy — ne pas éditer, il sera réécrit.",
            "[Service]",
            f"Environment=FJBK_OFFSITE_NODE={self.node}",
            f"Environment=FJBK_OFFSITE_SRC={source}",
        )) + "\n"

    def check(self, ctx) -> Outcome:
        source = ctx.facts.get("offsite_src")
        if not source:
            return Outcome(
                "error",
                "source hors-site non résolue — le drop-in n'est pas écrit "
                "plutôt que de porter un chemin deviné",
            )

        voulu = self._contenu(str(source))
        actuel = self.chemin.read_text() if self.chemin.is_file() else ""
        if actuel == voulu:
            return Outcome("ok", f"{self.chemin} (nœud {self.node}, {source})")
        return Outcome(
            "drift" if actuel else "absent",
            f"{self.chemin} — nœud {self.node}, source {source}",
            (
                Action(
                    f"écrire {self.chemin} (0644)",
                    lambda c, p=self.chemin, t=voulu:
                        c.fs.write_file(p, t, mode=0o644),
                    effects=frozenset({EFFET_RELOAD}),
                ),
            ),
        )
