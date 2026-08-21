"""Copie hors-site des sauvegardes vers GCS. **Tourne sur l'hôte, pas dans le CT.**

Port de `pgbk-offsite.sh`. Ce qui suit reprend les décisions du bash, qui sont
toutes le produit d'une panne ou d'un droit volontairement absent.

POURQUOI SUR L'HÔTE — le CT PostgreSQL est le composant le plus sensible du
nœud. Il n'a aucune raison de détenir des identifiants GCP ni d'atteindre
internet. L'hôte lit le dataset par sa **vue hôte** (`/data/subvol-200-disk-0`)
et non par sa vue CT (`/var/backups/postgresql`).

COPY, JAMAIS SYNC — `sync` réplique les suppressions : un dataset démonté et la
copie distante disparaît avec l'originale. La rétention distante est une règle
de cycle de vie du bucket, jamais l'affaire de ce code. `core.commands.Rclone`
n'expose délibérément ni `sync` ni `delete`.

DROITS DÉLIBÉRÉMENT INCOMPLETS — le compte de service est `objectViewer` +
`objectCreator` : il liste, lit et crée, il n'écrase ni ne supprime. Un nœud
compromis ne peut donc pas détruire l'historique. Conséquence directe : le
transfert est en `--ignore-existing`, et un objet partiel laissé par un
transfert interrompu est **signalé** comme anomalie, jamais réparé d'ici — et
surtout pas en boucle.

CODES DE RETOUR — ils sont un contrat avec systemd et avec les habitudes :

    0    tout est en ligne
    1    environnement inutilisable (rclone, clé, bucket, aucune sauvegarde)
    2    au moins un transfert a échoué
    3    au moins un objet distant diverge — intervention humaine
    130  interrompu par signal

Le bash faisait circuler un `10` (« transféré ») comme valeur de retour interne
de `push_snapshot`. Ce n'était jamais un code de sortie de processus, et il
disparaît ici : un test le vérifie. Le bash laissait aussi échapper des codes
arbitraires par son `trap ERR` (`exit $rc`) ; ils sont désormais rabattus
sur 1, « environnement inutilisable » étant la bonne famille pour un incident
imprévu.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from core.commands import Rclone, RcloneConfig
from core.log import CONT, detail, error, info, step, warn
from core.runner import CommandError, Runner

# ─── Codes de retour ─────────────────────────────────────────────────────────

EXIT_OK = 0
EXIT_ENV = 1
EXIT_FAILED = 2
EXIT_DIVERGENT = 3
EXIT_SIGNAL = 130


class Preflight(RuntimeError):
    """Environnement inutilisable. Toujours un code 1, jamais autre chose."""


class Snap(Enum):
    """Sort d'un instantané. Volontairement sans valeur numérique : le `10` du
    bash était une valeur de retour interne, il n'a pas à exister ici."""

    ONLINE = "déjà en ligne"
    TRANSFERRED = "transféré"
    FAILED = "en échec"
    DIVERGENT = "divergent"


# ─── Paramétrage ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OffsiteConfig:
    """Valeurs par défaut ici, valeurs réelles dans `pgbk-offsite.service`.

    L'unité systemd est l'endroit unique où l'on décrit ce nœud-ci, et
    `pg deploy` relit ses lignes `Environment=` pour vérifier ses prérequis.
    """

    node: str
    src: Path
    remote: str = "gcs"
    bucket: str = "homelab-pgsql-backups-dc93212a"
    subpath: str = "postgresql"
    config: Path = Path("/root/.config/rclone/rclone.conf")
    key: Path = Path("/root/.config/rclone/pgsql-backups.json")
    rclone: str = "/usr/bin/rclone"  # absolu : le PATH de systemd est minimal
    transfers: int = 4
    retries: int = 3
    bwlimit: str = ""
    check_mode: str = "hash"  # hash | size
    stale_hours: int = 48

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, hostname: str) -> "OffsiteConfig":
        def val(nom: str, defaut: str) -> str:
            brut = env.get(f"PGBK_OFFSITE_{nom}", "")
            return brut if brut else defaut

        defauts = cls(node="", src=Path("."))
        return cls(
            node=val("NODE", hostname),
            src=Path(val("SRC", "/data/subvol-200-disk-0")),
            remote=val("REMOTE", defauts.remote),
            bucket=val("BUCKET", defauts.bucket),
            subpath=val("SUBPATH", defauts.subpath),
            config=Path(val("CONFIG", str(defauts.config))),
            key=Path(val("KEY", str(defauts.key))),
            rclone=val("RCLONE", defauts.rclone),
            transfers=int(val("TRANSFERS", str(defauts.transfers))),
            retries=int(val("RETRIES", str(defauts.retries))),
            # Vide = pas de bridage. Le seul réglage dont la valeur vide est
            # significative, d'où le passage direct par env.get.
            bwlimit=env.get("PGBK_OFFSITE_BWLIMIT", ""),
            check_mode=val("CHECK", defauts.check_mode),
            stale_hours=int(val("STALE_HOURS", str(defauts.stale_hours))),
        )

    @property
    def prefix(self) -> str:
        """Le nœud au premier niveau, pour qu'un second s'ajoute sans
        restructurer l'arborescence distante."""
        return f"{self.node}/{self.subpath}"

    @property
    def base(self) -> str:
        return f"{self.remote}:{self.bucket}/{self.prefix}"

    def rclone_config(self) -> RcloneConfig:
        return RcloneConfig(
            remote=self.remote,
            bucket=self.bucket,
            config=self.config,
            binary=self.rclone,
            transfers=self.transfers,
            retries=self.retries,
            bwlimit=self.bwlimit,
            check_mode=self.check_mode,
        )


# ─── Inventaire local : des fonctions pures ──────────────────────────────────

# Trois choses ne partent jamais :
#   latest         symlink ABSOLU vers un chemin qui n'existe que dans le CT,
#                  donc cassé vu de l'hôte ;
#   pre-restore-*  filets posés par « pgbk restore » avant d'écraser une base,
#                  locaux et temporaires ;
#   *.part         exécution en cours ou interrompue. Par construction de
#                  pg-backup.sh, un répertoire SANS ce suffixe est complet.
EXCLUS = ("*.part", "pre-restore-*", "latest")


def local_snapshots(src: Path) -> list[Path]:
    """Les instantanés éligibles, dans l'ordre chronologique.

    L'horodatage `AAAAMMJJ-HHMMSS` fait coïncider ordre lexicographique et
    ordre chronologique : trier les noms suffit.
    """
    trouves = []
    for chemin in src.iterdir():
        if not chemin.is_dir() or not chemin.name.startswith("20"):
            continue
        if any(chemin.match(motif) for motif in EXCLUS):
            continue
        trouves.append(chemin)
    return sorted(trouves, key=lambda p: p.name)


def relative_files(racine: Path) -> list[str]:
    """Chemins relatifs des fichiers, triés — l'équivalent de
    `find -type f -printf '%P\\n' | sort`."""
    return sorted(
        str(p.relative_to(racine)) for p in racine.rglob("*") if p.is_file()
    )


@dataclass(frozen=True)
class SnapshotDiff:
    """La comparaison local/distant, réduite à ce qu'elle est : deux listes.

    Fonction pure du contenu, donc testable sans bucket ni conteneur.
    """

    name: str
    local: tuple[str, ...]
    remote: tuple[str, ...]

    @property
    def missing(self) -> tuple[str, ...]:
        """Différence d'ensembles triée — l'équivalent de `comm -23`."""
        return tuple(sorted(set(self.local) - set(self.remote)))

    @property
    def empty(self) -> bool:
        return not self.local

    @property
    def has_manifest(self) -> bool:
        return "MANIFEST" in self.local


def age_hours(chemin: Path, maintenant: float) -> int:
    """Âge en heures pleines, sur le mtime — comme `stat -c %Y` du bash."""
    return int((maintenant - chemin.stat().st_mtime) // 3600)


# ─── Contrôles préalables ────────────────────────────────────────────────────


def preflight(cfg: OffsiteConfig, *, euid: int) -> None:
    """Tout ce qui peut manquer est vérifié AVANT le premier transfert.

    Un échec silencieux est pire qu'une absence de sauvegarde : on croirait
    avoir une copie hors-site.
    """
    if euid != 0:
        raise Preflight(
            "à lancer en root sur le nœud : les dumps sont en 600, "
            "propriété d'un UID de CT non privilégié"
        )
    if not os.access(cfg.rclone, os.X_OK):
        raise Preflight(
            f"rclone introuvable : {cfg.rclone} — l'installer : apt install rclone"
        )
    if not os.access(cfg.config, os.R_OK):
        raise Preflight(
            f"configuration rclone absente ou illisible : {cfg.config}"
        )
    if not (cfg.key.is_file() and cfg.key.stat().st_size > 0):
        raise Preflight(
            f"clé du compte de service absente ou vide : {cfg.key}\n"
            f"{CONT}elle est hors dépôt par construction — "
            "la reposer depuis le gestionnaire de secrets"
        )
    # Une clé tronquée ou un fichier collé de travers se voit ici, avant de
    # partir sur un 401 incompréhensible.
    if '"private_key"' not in cfg.key.read_text(encoding="utf-8", errors="replace"):
        raise Preflight(
            f"clé invalide : {cfg.key} ne contient pas de champ private_key"
        )
    if not cfg.src.is_dir():
        raise Preflight(
            f"source absente : {cfg.src}\n"
            f"{CONT}c'est la VUE HÔTE du dataset ; dans le CT il s'appelle "
            "/var/backups/postgresql\n"
            f"{CONT}vérifier que le CT est démarré et le dataset monté"
        )


# ─── Transfert d'un instantané ───────────────────────────────────────────────


def push_snapshot(
    rc: Rclone, cfg: OffsiteConfig, dossier: Path, *, dry_run: bool
) -> Snap:
    """Met un instantané en ligne. Ne lève pas : renvoie son sort.

    Un instantané en échec n'arrête pas les autres — mieux vaut sauver les neuf
    qui passent et signaler le dixième.
    """
    nom = dossier.name
    dest = f"{cfg.base}/{nom}"

    locaux = relative_files(dossier)
    if not locaux:
        warn(f"  {nom} : répertoire vide — ignoré")
        return Snap.ONLINE

    try:
        distants = rc.list_files(dest)
    except CommandError as exc:
        # Le nœud pourra réessayer : c'est un échec, pas une anomalie.
        error(f"  {nom} : listage distant impossible")
        detail(exc.result.stdout + exc.result.stderr)
        return Snap.FAILED

    diff = SnapshotDiff(nom, tuple(locaux), tuple(distants))

    # Un instantané complet porte toujours son MANIFEST. Son absence n'empêche
    # pas la copie — des dumps sans manifeste valent mieux que rien — mais elle
    # mérite d'être dite.
    if not diff.has_manifest:
        warn(f"  {nom} : pas de MANIFEST")

    manquants = diff.missing
    if not manquants:
        info(f"  {nom} : {len(diff.local)} objet(s) déjà en ligne")
    else:
        info(f"  {nom} : {len(manquants)}/{len(diff.local)} objet(s) à envoyer")
        if dry_run:
            detail("\n".join(manquants))
            # Le contrôle porterait sur un instantané incomplet et échouerait
            # pour la mauvaise raison : on ne peut rien conclure sur une
            # éventuelle divergence tant que ces objets ne sont pas partis.
            warn(f"  {nom} : divergence non évaluable ({len(manquants)} objet(s) manquant(s))")
            return Snap.TRANSFERRED
        try:
            rc.copy(dossier, dest)
        except CommandError:
            error(f"  {nom} : transfert en échec")
            return Snap.FAILED

    # ─── Contrôle post-transfert ──────────────────────────────────────────────
    # Il porte sur TOUT l'instantané, pas seulement sur ce qui vient d'être
    # envoyé : c'est ici, et nulle part ailleurs, qu'un objet partiel laissé par
    # une exécution interrompue se révèle.
    #
    # Le bash sortait avant ce contrôle en --dry-run, ce qui rendait la
    # simulation aveugle au seul mode de panne autour duquel tout ce montage est
    # conçu. Il est joué ici aussi : c'est une lecture, elle n'écrit rien.
    conforme, sortie = rc.check(dossier, dest)
    if not conforme:
        error(f"  {nom} : le distant DIVERGE de la source")
        detail(sortie)
        error("  ces objets ne peuvent pas être corrigés depuis ce nœud : le compte de")
        error("  service n'a pas le droit d'écraser (objectCreator sans objects.delete).")
        error("  INTERVENTION HUMAINE, depuis un poste avec le compte personnel :")
        error(f"    gcloud storage rm gs://{cfg.bucket}/{cfg.prefix}/{nom}/<objet>")
        error("  puis rejouer : systemctl start pgbk-offsite.service")
        return Snap.DIVERGENT

    if not manquants:
        return Snap.ONLINE
    info(f"  {nom} : contrôle OK, {len(manquants)} objet(s) transféré(s)")
    return Snap.TRANSFERRED


# ─── Verdict ─────────────────────────────────────────────────────────────────


def verdict(sorts: Iterable[Snap]) -> int:
    """Du décompte au code de retour.

    La divergence est testée AVANT l'échec : un transfert raté se rejoue tout
    seul à la prochaine exécution, un objet distant divergent demande une
    intervention humaine et ne doit pas être masqué par un échec transitoire.
    """
    compte = {sort: 0 for sort in Snap}
    for sort in sorts:
        compte[sort] += 1
    if compte[Snap.DIVERGENT]:
        return EXIT_DIVERGENT
    if compte[Snap.FAILED]:
        return EXIT_FAILED
    return EXIT_OK


# ─── Exécution ───────────────────────────────────────────────────────────────


def run(cfg: OffsiteConfig, runner: Runner, *, dry_run: bool, now: float) -> int:
    """Le déroulé complet. Renvoie le code de retour, ne quitte jamais."""
    step(f"démarrage — {cfg.src} → {cfg.base}")

    try:
        preflight(cfg, euid=os.geteuid())
    except Preflight as exc:
        for ligne in str(exc).splitlines():
            error(ligne)
        return EXIT_ENV

    rc = Rclone(runner, cfg.rclone_config())
    info(
        f"  rclone {rc.version} | {cfg.transfers} transfert(s) parallèle(s) "
        f"| contrôle par {cfg.check_mode}"
    )
    if dry_run:
        warn("  --dry-run : aucun objet ne sera écrit")

    # ─── Joignabilité du bucket ───────────────────────────────────────────────
    # Un listage suffit à prouver que la clé est valide, que le réseau passe et
    # que le bucket existe.
    step("joignabilité du bucket")
    joignable, sortie = rc.reachable()
    if not joignable:
        error(f"bucket injoignable : {cfg.remote}:{cfg.bucket}")
        detail(sortie)
        error("causes usuelles : clé révoquée, droits IAM retirés, pas de sortie internet")
        return EXIT_ENV
    info(f"  OK — {len(sortie.splitlines()) if sortie else 0} entrée(s) à la racine du bucket")

    # ─── Inventaire local ─────────────────────────────────────────────────────
    step("inventaire local")
    instantanes = local_snapshots(cfg.src)
    if not instantanes:
        error(f"aucune sauvegarde locale dans {cfg.src}")
        error(
            "rien à copier hors-site — vérifier le timer du CT : "
            "pct exec 200 -- systemctl status pg-backup.timer"
        )
        return EXIT_ENV
    info(
        f"  {len(instantanes)} instantané(s) éligible(s), "
        f"du {instantanes[0].name} au {instantanes[-1].name}"
    )

    # Une source qui ne bouge plus produirait des exécutions parfaitement
    # vertes tout en ne protégeant plus rien. Le dire, sans faire échouer : le
    # hors-site n'est pas responsable de la sauvegarde locale.
    age = age_hours(instantanes[-1], now)
    if age > cfg.stale_hours:
        warn(f"  le dernier instantané local a {age} h (seuil {cfg.stale_hours} h)")
        warn("  la sauvegarde du CT ne tourne peut-être plus")

    # ─── Boucle principale ────────────────────────────────────────────────────
    step(f"copie vers {cfg.base}")
    sorts = [
        push_snapshot(rc, cfg, dossier, dry_run=dry_run) for dossier in instantanes
    ]

    # ─── Bilan ────────────────────────────────────────────────────────────────
    compte = {sort: sorts.count(sort) for sort in Snap}
    # En simulation, « transféré » compte ce qui PARTIRAIT : le dire, sinon le
    # bilan se lit comme une exécution réelle.
    verbe = "à transférer" if dry_run else "transféré(s)"
    step(
        f"bilan — {compte[Snap.TRANSFERRED]} {verbe}, "
        f"{compte[Snap.ONLINE]} déjà en ligne, "
        f"{compte[Snap.FAILED]} en échec, "
        f"{compte[Snap.DIVERGENT]} divergent(s)"
    )
    if not dry_run:
        taille = rc.size(cfg.base)
        if taille:
            info(f"  distant : {taille}")

    code = verdict(sorts)
    if code == EXIT_DIVERGENT:
        error(f"{compte[Snap.DIVERGENT]} instantané(s) divergent(s), voir ci-dessus")
        error("la copie hors-site est INCOMPLÈTE tant que ce n'est pas traité à la main")
    elif code == EXIT_FAILED:
        error(f"{compte[Snap.FAILED]} instantané(s) en échec")
    else:
        step(f"terminé — {len(instantanes)} instantané(s) en ligne sur {cfg.base}")
    return code
