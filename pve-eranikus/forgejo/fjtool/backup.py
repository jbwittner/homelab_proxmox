"""Sauvegarde logique de Forgejo. **Tourne DANS le conteneur.**

    /var/backups/forgejo/
      20260821-024500/          ← une exécution = un répertoire
        forgejo.dump            ← la base, format -Fc
        app.ini                 ← la configuration RÉELLEMENT en service
        MANIFEST                ← de quoi savoir si un vzdump lui correspond
      latest -> 20260821-024500

CE QUI N'EST PAS LÀ-DEDANS, ET POURQUOI. Les dépôts, les objets LFS et les
pièces jointes n'y sont pas : `/var/lib/forgejo` pèse des dizaines de
gigaoctets, et le tarer chaque nuit à côté d'une base de quelques centaines de
mégaoctets remplirait le volume pour une redondance que `vzdump` assure déjà.
Le partage est donc :

    la BASE part par ici       — quotidien, granulaire, restaurable seule ;
    les DÉPÔTS partent par vzdump du CT (doc/RUNBOOK.md section 9).

D'où le MANIFEST. **Restaurer Forgejo demande LES DEUX**, pris à des instants
proches : une base qui référence un dépôt absent du disque, ou l'inverse,
donne une instance qui démarre et se comporte n'importe comment. Le manifeste
consigne l'état de l'arborescence AU MOMENT DU DUMP — nombre de dépôts, octets
occupés, date de la modification la plus récente. Au moment d'une reprise,
c'est ce qui permet de dire si le vzdump retenu correspond au dump retenu, au
lieu de l'espérer.

ATOMICITÉ — tout est écrit dans `<stamp>.part/`, renommé en `<stamp>/`
seulement si l'exécution va au bout. Un répertoire présent est donc, par
construction, une sauvegarde complète. Une exécution interrompue ne laisse
rien qu'une copie hors-site pourrait prendre pour bonne.

CONTRÔLE D'ESPACE — le script refuse de démarrer s'il ne peut pas garantir
`MIN_FREE_MB` libres à l'arrivée. Le volume est distinct de celui des dépôts,
mais le saturer ferait échouer toutes les sauvegardes suivantes en silence.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping

from core.commands import Psql
from core.log import CONT, error, info, step, warn
from core.runner import CommandError, Runner

# Codes de retour — un contrat avec systemd et avec les habitudes.
EXIT_OK = 0
EXIT_ENV = 1        # environnement inutilisable : espace, base injoignable
EXIT_ECHEC = 2      # la sauvegarde elle-même a échoué

BASE = "forgejo"
APP_INI = Path("/etc/forgejo/app.ini")
DEPOTS = Path("/var/lib/forgejo/repositories")


@dataclass(frozen=True)
class Config:
    dest: Path = Path("/var/backups/forgejo")
    retention: int = 14
    min_free_mb: int = 512

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        """L'unité systemd est l'endroit unique où ce nœud-ci est décrit.

        Une valeur absente reprend le défaut ; une valeur illisible est un
        refus, pas un repli silencieux — un `FJ_BACKUP_RETENTION=quatorze`
        qui retomberait sur 14 donnerait l'illusion d'avoir été lu.
        """
        def entier(nom: str, defaut: int) -> int:
            brut = env.get(nom)
            if brut is None or brut == "":
                return defaut
            try:
                return int(brut)
            except ValueError:
                raise SystemExit(f"{nom} n'est pas un entier : {brut}") from None

        return cls(
            dest=Path(env.get("FJ_BACKUP_DEST") or cls.dest),
            retention=entier("FJ_BACKUP_RETENTION", cls.retention),
            min_free_mb=entier("FJ_BACKUP_MIN_FREE_MB", cls.min_free_mb),
        )


def horodatage(maintenant: datetime | None = None) -> str:
    """`20260821-024500`. Triable en ordre lexicographique, ce qui est
    exactement ce dont `latest` et l'élagage ont besoin."""
    return (maintenant or datetime.now()).strftime("%Y%m%d-%H%M%S")


def espace_libre_mb(chemin: Path) -> int:
    usage = shutil.disk_usage(chemin)
    return usage.free // (1024 * 1024)


@dataclass(frozen=True)
class EtatDepots:
    """L'état de `/var/lib/forgejo/repositories`, en trois nombres.

    Trois nombres et pas une empreinte : hacher des dizaines de gigaoctets à
    2h45 coûterait plus que la sauvegarde elle-même. Ces trois-là suffisent à
    répondre à la seule question qui se pose en reprise — « ce vzdump est-il
    du même moment que ce dump ? » — et à la répondre par « non » quand c'est
    non, ce qui est le sens utile.
    """

    depots: int
    octets: int
    dernier_mtime: int

    @classmethod
    def relever(cls, racine: Path = DEPOTS) -> "EtatDepots":
        if not racine.is_dir():
            return cls(0, 0, 0)
        depots = 0
        octets = 0
        dernier = 0
        for chemin in racine.rglob("*"):
            try:
                info_fichier = chemin.lstat()
            except OSError:
                # Un dépôt supprimé pendant le parcours n'est pas une panne de
                # sauvegarde : on ne le compte pas, et on continue.
                continue
            if chemin.is_dir() and chemin.name.endswith(".git"):
                depots += 1
            octets += info_fichier.st_size
            dernier = max(dernier, int(info_fichier.st_mtime))
        return cls(depots, octets, dernier)


def rendre_manifeste(
    *,
    stamp: str,
    version: str,
    etat: EtatDepots,
    taille_dump: int,
) -> str:
    """Un format `CLÉ=valeur`, lisible par un humain comme par un script.

    Surtout pas du JSON ici : ce fichier se lit en reprise, parfois depuis un
    shell de secours sans `jq`, et une accolade mal placée ne doit jamais
    empêcher d'en tirer une date.
    """
    return "\n".join((
        "# Sauvegarde Forgejo — voir doc/RUNBOOK.md section 9.",
        "# LES DÉPÔTS NE SONT PAS DANS CETTE SAUVEGARDE : ils partent par",
        "# vzdump. Les trois lignes REPOS_* décrivent leur état AU MOMENT du",
        "# dump, pour savoir quel vzdump correspond à celui-ci.",
        f"STAMP={stamp}",
        f"FORGEJO_VERSION={version}",
        f"DATABASE={BASE}",
        f"DUMP_BYTES={taille_dump}",
        f"REPOS_COUNT={etat.depots}",
        f"REPOS_BYTES={etat.octets}",
        f"REPOS_LAST_MTIME={etat.dernier_mtime}",
    )) + "\n"


def instantanes(dest: Path) -> list[Path]:
    """Les répertoires complets, du plus ancien au plus récent.

    Les `.part` sont exclus : ce sont des exécutions interrompues, et les
    compter comme des sauvegardes ferait croire à un filet qui n'existe pas.
    """
    if not dest.is_dir():
        return []
    return sorted(
        chemin for chemin in dest.iterdir()
        if chemin.is_dir()
        and chemin.name.startswith("20")
        and not chemin.name.endswith(".part")
    )


def a_elaguer(existants: list[Path], retention: int) -> list[Path]:
    """Ce qui dépasse la rétention — **jamais le dernier**.

    La garde sur le dernier n'est pas redondante avec la rétention : une
    rétention réglée à 0 par erreur, ou une horloge qui saute, effacerait tout
    ce qui reste. Une source de vérité sans aucune sauvegarde est le seul état
    dont on ne se relève pas.
    """
    if retention <= 0 or len(existants) <= 1:
        return []
    surplus = len(existants) - retention
    if surplus <= 0:
        return []
    return existants[:min(surplus, len(existants) - 1)]


# ─── l'exécution ─────────────────────────────────────────────────────────────


def executer(cfg: Config, runner: Runner, *, maintenant=None) -> int:
    """Une sauvegarde. Renvoie le code de retour."""
    step(f"sauvegarde de Forgejo vers {cfg.dest}")

    if not cfg.dest.is_dir():
        error(f"{cfg.dest} n'existe pas — le volume mp2 est-il monté ?")
        error(f"{CONT}sur le nœud : fj deploy")
        return EXIT_ENV

    libre = espace_libre_mb(cfg.dest)
    if libre < cfg.min_free_mb:
        error(
            f"{libre} Mo libres sur {cfg.dest}, {cfg.min_free_mb} Mo exigés — "
            "aucune sauvegarde n'est tentée"
        )
        return EXIT_ENV

    psql = Psql(runner)
    try:
        if not psql.database_exists(BASE):
            error(f"la base {BASE} n'existe pas — rien à sauvegarder")
            return EXIT_ENV
    except CommandError as exc:
        error(f"PostgreSQL injoignable : {exc.result.stderr.strip()[:200]}")
        return EXIT_ENV

    stamp = horodatage(maintenant)
    partiel = cfg.dest / f"{stamp}.part"
    final = cfg.dest / stamp
    if final.exists():
        warn(f"{final} existe déjà — rien n'est écrasé")
        return EXIT_OK

    partiel.mkdir(parents=True, exist_ok=True)
    try:
        info(f"  dump de {BASE}")
        dump = partiel / f"{BASE}.dump"
        psql.dump(BASE, dump)

        # La configuration EFFECTIVE, pas celle du dépôt : c'est elle qui
        # décrit l'instance qu'on restaurera, et elle a pu diverger.
        if APP_INI.is_file():
            (partiel / "app.ini").write_bytes(APP_INI.read_bytes())
        else:
            warn(f"{APP_INI} absent — la sauvegarde n'aura pas la configuration")

        info("  relevé de l'arborescence des dépôts")
        etat = EtatDepots.relever()
        (partiel / "MANIFEST").write_text(
            rendre_manifeste(
                stamp=stamp,
                version=_version_forgejo(runner),
                etat=etat,
                taille_dump=dump.stat().st_size,
            )
        )
    except (CommandError, OSError) as exc:
        error(f"sauvegarde échouée : {exc}")
        shutil.rmtree(partiel, ignore_errors=True)
        return EXIT_ECHEC

    # Le renommage EST la garantie d'atomicité : à partir d'ici, et pas avant,
    # le répertoire est une sauvegarde complète.
    partiel.rename(final)
    info(f"  {final} — dump {dump.stat().st_size} octets, "
         f"{etat.depots} dépôt(s) sur disque")

    _pointer_latest(cfg.dest, stamp)
    _elaguer(cfg)
    step(f"terminé — {stamp}")
    return EXIT_OK


def _version_forgejo(runner: Runner) -> str:
    from fjtool import version as V
    from fjtool.deploy import CT_BINAIRE

    res = runner.read(CT_BINAIRE, "--version", check=False)
    return (V.version_installee(res.stdout) if res.ok else None) or "inconnue"


def _pointer_latest(dest: Path, stamp: str) -> None:
    """`latest` est un lien RELATIF.

    Absolu, il pointerait sur `/var/backups/forgejo/...` — la vue CONTENEUR du
    volume. Lu depuis le nœud, qui voit le même dataset sous un autre chemin,
    il ne résoudrait rien. Relatif, il marche des deux côtés de la frontière.
    """
    lien = dest / "latest"
    if lien.is_symlink() or lien.exists():
        lien.unlink()
    lien.symlink_to(stamp)


def _elaguer(cfg: Config) -> None:
    for chemin in a_elaguer(instantanes(cfg.dest), cfg.retention):
        info(f"  élagage : {chemin.name}")
        shutil.rmtree(chemin, ignore_errors=True)


# ─── fj list ─────────────────────────────────────────────────────────────────


def lire_manifeste_texte(texte: str) -> dict[str, str]:
    """L'analyse, séparée de la lecture du fichier.

    Séparée pour que l'aller-retour « ce qu'on écrit / ce qu'on relit » se
    vérifie sans toucher au disque : c'est le seul contrôle qui empêche le
    format d'écriture et le format de lecture de diverger, et une divergence
    ici ne se verrait qu'un jour de reprise.
    """
    valeurs: dict[str, str] = {}
    for ligne in texte.splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        cle, _, valeur = ligne.partition("=")
        valeurs[cle.strip()] = valeur.strip()
    return valeurs


def lire_manifeste(chemin: Path) -> dict[str, str]:
    if not chemin.is_file():
        return {}
    return lire_manifeste_texte(chemin.read_text(encoding="utf-8"))


def rendre_liste(dest: Path, *, maintenant: float | None = None) -> str:
    """Le tableau des instantanés. Une DONNÉE : aucun horodatage de journal."""
    import time

    lignes = [f"{'INSTANTANÉ':<18} {'ÂGE':>7} {'DUMP':>10} {'DÉPÔTS':>7}  VERSION"]
    maintenant = maintenant if maintenant is not None else time.time()
    for chemin in reversed(instantanes(dest)):
        manifeste = lire_manifeste(chemin / "MANIFEST")
        age_h = int((maintenant - chemin.stat().st_mtime) // 3600)
        lignes.append(
            f"{chemin.name:<18} {age_h:>6}h "
            f"{_taille(manifeste.get('DUMP_BYTES')):>10} "
            f"{manifeste.get('REPOS_COUNT', '?'):>7}  "
            f"{manifeste.get('FORGEJO_VERSION', '?')}"
        )
    if len(lignes) == 1:
        lignes.append("  (aucun instantané)")
    return "\n".join(lignes)


def _taille(octets: str | None) -> str:
    """Comme `du -h` : une décimale en dessous de 10, un entier au-delà."""
    try:
        valeur = float(octets or 0)
    except ValueError:
        return "?"
    for unite in ("o", "K", "M", "G", "T"):
        if valeur < 1024 or unite == "T":
            return f"{valeur:.1f}{unite}" if valeur < 10 else f"{valeur:.0f}{unite}"
        valeur /= 1024
    return "?"
