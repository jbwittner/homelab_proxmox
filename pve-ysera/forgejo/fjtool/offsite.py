"""Copie hors-site des sauvegardes Forgejo vers GCS. **Tourne sur l'hôte.**

POURQUOI SUR L'HÔTE — le conteneur Forgejo est la source de vérité d'ArgoCD.
Il n'a aucune raison de détenir des identifiants GCP ni d'atteindre internet
en écriture. L'hôte lit le dataset des sauvegardes par sa **vue hôte**
(`/data/subvol-400-disk-0`, résolue par `pvesm`) et non par sa vue conteneur
(`/var/backups/forgejo`).

COPY, JAMAIS SYNC — `sync` réplique les suppressions : un dataset démonté et la
copie distante disparaît avec l'originale. La rétention distante est une règle
de cycle de vie du bucket, jamais l'affaire de ce code. `core.commands.Rclone`
n'expose délibérément ni `sync` ni `delete`, et cette absence EST la garantie.

DROITS DÉLIBÉRÉMENT INCOMPLETS — le compte de service est `objectViewer` +
`objectCreator` : il liste, lit et crée, il n'écrase ni ne supprime. Un nœud
compromis ne peut donc pas détruire l'historique. Conséquence directe : le
transfert est en `--ignore-existing`, et un objet distant qui diverge est
**signalé**, jamais réparé d'ici — et surtout pas en boucle.

CODES DE RETOUR — un contrat avec systemd et avec les habitudes :

    0    tout est en ligne
    1    environnement inutilisable (rclone, clé, bucket, aucune sauvegarde)
    2    au moins un transfert a échoué
    3    au moins un objet distant diverge — intervention humaine
    130  interrompu par signal

Une faute de frappe dans les arguments sort en **1** et non en 2 : dans cette
table, 2 veut dire « transfert en échec », et une commande mal tapée se
lirait alors comme une panne de copie trois semaines plus tard. Défaut
constaté sur `pg offsite` le 21 août 2026.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from core.commands import Rclone, RcloneConfig
from core.log import CONT, detail, error, info, step, warn
from core.runner import CommandError, Runner

EXIT_OK = 0
EXIT_ENV = 1
EXIT_FAILED = 2
EXIT_DIVERGENT = 3
EXIT_SIGNAL = 130


class Preflight(RuntimeError):
    """Environnement inutilisable. Toujours un code 1, jamais autre chose."""


class Sort(Enum):
    """Sort d'un instantané. Sans valeur numérique : ce ne sont pas des codes
    de sortie de processus, et les confondre a déjà coûté un diagnostic."""

    EN_LIGNE = "déjà en ligne"
    TRANSFERE = "transféré"
    ECHEC = "en échec"
    DIVERGENT = "divergent"


@dataclass(frozen=True)
class OffsiteConfig:
    """Valeurs par défaut ici, valeurs réelles dans `fjbk-offsite.service`.

    L'unité systemd est l'endroit unique où l'on décrit ce nœud-ci, et
    `fj deploy` relit ses lignes `Environment=` pour vérifier ses prérequis.
    """

    node: str
    src: Path
    remote: str = "gcs"
    bucket: str = "homelab-pgsql-backups-dc93212a"
    subpath: str = "forgejo"
    config: Path = Path("/root/.config/rclone/rclone.conf")
    key: Path = Path("/root/.config/rclone/pgsql-backups.json")
    binary: str = "/usr/bin/rclone"
    transfers: int = 4
    check_mode: str = "hash"
    stale_hours: int = 48

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, hostname: str) -> "OffsiteConfig":
        def entier(nom: str, defaut: int) -> int:
            brut = env.get(nom)
            if not brut:
                return defaut
            try:
                return int(brut)
            except ValueError:
                raise Preflight(f"{nom} n'est pas un entier : {brut}") from None

        return cls(
            node=env.get("FJBK_OFFSITE_NODE") or hostname,
            src=Path(env.get("FJBK_OFFSITE_SRC") or "/var/backups/forgejo"),
            remote=env.get("FJBK_OFFSITE_REMOTE") or cls.remote,
            bucket=env.get("FJBK_OFFSITE_BUCKET") or cls.bucket,
            subpath=env.get("FJBK_OFFSITE_SUBPATH") or cls.subpath,
            config=Path(env.get("FJBK_OFFSITE_CONFIG") or cls.config),
            key=Path(env.get("FJBK_OFFSITE_KEY") or cls.key),
            binary=env.get("FJBK_OFFSITE_RCLONE") or cls.binary,
            transfers=entier("FJBK_OFFSITE_TRANSFERS", cls.transfers),
            check_mode=env.get("FJBK_OFFSITE_CHECK") or cls.check_mode,
            stale_hours=entier("FJBK_OFFSITE_STALE_HOURS", cls.stale_hours),
        )

    @property
    def prefix(self) -> str:
        """`<nœud>/<service>` — l'arborescence distante suit la locale.

        Un bucket partagé entre plusieurs nœuds et plusieurs services n'est
        lisible que si le chemin dit d'où vient chaque objet.
        """
        return f"{self.node}/{self.subpath}"

    def rclone_config(self) -> RcloneConfig:
        return RcloneConfig(
            remote=self.remote,
            bucket=self.bucket,
            config=self.config,
            binary=self.binary,
            transfers=self.transfers,
            check_mode=self.check_mode,
        )


# ─── ce qui est là, localement ───────────────────────────────────────────────


def instantanes_locaux(src: Path) -> list[Path]:
    """Les répertoires COMPLETS, du plus ancien au plus récent.

    Les `.part` sont exclus : ce sont des exécutions interrompues. En copier
    un mettrait dans le bucket un objet que le compte de service ne pourra
    jamais remplacer — la version tronquée y resterait pour toujours.
    """
    if not src.is_dir():
        return []
    return sorted(
        chemin for chemin in src.iterdir()
        if chemin.is_dir()
        and chemin.name.startswith("20")
        and not chemin.name.endswith(".part")
    )


def fichiers_relatifs(racine: Path) -> list[str]:
    return sorted(
        str(chemin.relative_to(racine))
        for chemin in racine.rglob("*")
        if chemin.is_file()
    )


def age_heures(chemin: Path, maintenant: float) -> int:
    return int((maintenant - chemin.stat().st_mtime) // 3600)


# ─── contrôles préalables ────────────────────────────────────────────────────


def preflight(cfg: OffsiteConfig, *, euid: int) -> None:
    """Tout ce qui rend la copie impossible, constaté AVANT de commencer.

    Chaque refus dit quoi faire. Un `rclone` absent et un bucket injoignable
    demandent deux gestes différents ; les confondre dans un « échec » unique
    obligerait à relire le journal ligne à ligne.
    """
    if euid != 0:
        raise Preflight(
            "à lancer en root sur le nœud — les dumps appartiennent à un UID "
            "de conteneur non privilégié et ne sont lisibles que par root"
        )
    if not Path(cfg.binary).is_file():
        raise Preflight(
            f"{cfg.binary} absent — l'installer : fj deploy"
        )
    if not (cfg.key.is_file() and cfg.key.stat().st_size > 0):
        raise Preflight(
            f"clé du compte de service absente ou vide : {cfg.key}\n"
            f"{CONT}c'est un secret, hors dépôt par construction — "
            "la reposer depuis OpenBao"
        )
    if not cfg.config.is_file():
        raise Preflight(f"{cfg.config} absent — le poser : fj deploy")
    if not cfg.src.is_dir():
        raise Preflight(
            f"{cfg.src} n'est pas un répertoire — c'est la VUE HÔTE du volume "
            f"de sauvegarde du CT, résolue par pvesm ; « fj deploy » l'inscrit "
            "dans le drop-in du nœud"
        )


# ─── le transfert ────────────────────────────────────────────────────────────


def pousser(
    rclone: Rclone, local: Path, distant: str, *, dry_run: bool
) -> Sort:
    """Un instantané. Renvoie son sort, sans jamais lever.

    L'ORDRE DES CONSTATS COMPTE. On liste le distant AVANT de copier : c'est
    ce qui distingue « déjà en ligne » de « transféré », et cette distinction
    est la seule information utile d'un journal de copie quotidienne — sans
    elle, chaque nuit se ressemble et une copie qui ne part plus ne se voit
    pas.
    """
    attendus = fichiers_relatifs(local)
    if not attendus:
        warn(f"  {local.name} : vide, ignoré")
        return Sort.ECHEC

    try:
        presents = set(rclone.list_files(distant))
    except CommandError as exc:
        error(f"  {local.name} : listage distant impossible")
        detail(exc.result.stderr.strip())
        # Réessayer demain plutôt que de conclure « rien à copier » : un
        # listage en échec ne dit RIEN sur ce qui est là-bas.
        return Sort.ECHEC

    manquants = [nom for nom in attendus if nom not in presents]

    if not manquants:
        conforme, message = rclone.check(local, distant)
        if conforme:
            info(f"  {local.name} : {Sort.EN_LIGNE.value}")
            return Sort.EN_LIGNE
        error(
            f"  {local.name} : objet distant divergent — le compte de service "
            "ne peut pas écraser, il faut intervenir à la main "
            "(doc/RUNBOOK.md section 10)"
        )
        detail(message)
        return Sort.DIVERGENT

    if dry_run:
        info(f"  {local.name} : {len(manquants)} fichier(s) à transférer")
        for nom in manquants:
            info(f"{CONT}{nom}")
        return Sort.TRANSFERE

    step(f"  {local.name} : transfert de {len(manquants)} fichier(s)")
    try:
        rclone.copy(local, distant)
    except CommandError as exc:
        error(f"  {local.name} : transfert échoué (code {exc.result.code})")
        return Sort.ECHEC

    # Vérifié APRÈS coup, et pas seulement « la copie a rendu 0 ». Un objet
    # partiel laissé par une exécution interrompue ne se révèle qu'ici.
    conforme, message = rclone.check(local, distant)
    if not conforme:
        error(f"  {local.name} : divergent après transfert")
        detail(message)
        return Sort.DIVERGENT
    return Sort.TRANSFERE


def verdict(sorts: Iterable[Sort]) -> int:
    """Le code de sortie, à partir de tous les sorts.

    La divergence l'emporte sur l'échec : un transfert raté sera retenté
    demain tout seul, un objet divergent ne se réparera jamais de lui-même et
    demande quelqu'un. Le code le plus élevé doit désigner ce qui ne se
    résoudra pas sans intervention.
    """
    sorts = list(sorts)
    if not sorts:
        return EXIT_ENV
    if any(sort is Sort.DIVERGENT for sort in sorts):
        return EXIT_DIVERGENT
    if any(sort is Sort.ECHEC for sort in sorts):
        return EXIT_FAILED
    return EXIT_OK


def run(
    cfg: OffsiteConfig, runner: Runner, *, dry_run: bool, now: float
) -> int:
    debut = time.monotonic()
    try:
        preflight(cfg, euid=_euid())
    except Preflight as refus:
        error(str(refus))
        return EXIT_ENV

    rclone = Rclone(runner, cfg.rclone_config())
    joignable, message = rclone.reachable()
    if not joignable:
        error(f"bucket injoignable : {cfg.bucket}")
        detail(message)
        return EXIT_ENV

    locaux = instantanes_locaux(cfg.src)
    if not locaux:
        error(
            f"aucune sauvegarde dans {cfg.src} — "
            "le timer du conteneur tourne-t-il ? "
            f"(pct exec <ctid> -- systemctl status fj-backup.timer)"
        )
        return EXIT_ENV

    # Une sauvegarde trop vieille est une alarme, pas un détail : la copie
    # peut très bien réussir sur un instantané périmé, et tout paraîtrait
    # normal.
    age = age_heures(locaux[-1], now)
    if age > cfg.stale_hours:
        warn(
            f"la sauvegarde la plus récente a {age} h "
            f"(seuil {cfg.stale_hours} h) — la copie continue, mais elle ne "
            "porte pas sur des données fraîches"
        )

    step(
        f"copie hors-site : {len(locaux)} instantané(s) vers "
        f"{rclone.path(cfg.prefix)}"
    )
    if dry_run:
        info("(mode --dry-run : aucun transfert)")

    sorts = [
        pousser(
            rclone,
            chemin,
            rclone.path(cfg.prefix, chemin.name),
            dry_run=dry_run,
        )
        for chemin in locaux
    ]

    duree = int(time.monotonic() - debut)
    code = verdict(sorts)
    # La DURÉE reste dans le verdict : une copie qui passe de 2 s à 40 min ne
    # se voit que là. Elle avait disparu de `pg offsite`, constaté le
    # 21 août 2026.
    step(f"terminé en {duree}s — code {code}")
    return code


def _euid() -> int:
    import os

    return os.geteuid()
