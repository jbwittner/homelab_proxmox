"""Sections D et E — l'outillage et les paquets du nœud.

Les plus mécaniques, portées en premier : elles n'ont ni protection à lever, ni
redémarrage à provoquer, et servent donc de banc d'essai au moteur.

DEUX PRINCIPES, VISIBLES DANS CHAQUE ÉTAPE.

**Un drapeau ne désactive jamais un contrôle, seulement une pose.**
`--no-install` n'empêche pas de constater qu'un paquet manque : il empêche de
l'installer, et l'étape sort alors en `error` plutôt qu'en `absent`. C'est ce
qui permet à `--status` de rester complet quels que soient les drapeaux, et
c'est la règle « ne jamais armer un automatisme dont les prérequis manquent »
appliquée en amont.

**On ne propose que ce qu'on saurait faire.** Une version de Python trop
ancienne n'est pas une dérive à corriger : c'est un constat. L'étape le dit et
ne fabrique aucune action, plutôt que d'en inventer une qui échouerait.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from core.converge import Action, Outcome
from proxmox import diff_tree

# Version minimale, la même que celle contrôlée par le lanceur. Deux seuils
# différents donneraient un déploiement vert et un « pg » qui refuse de tourner.
from core import MIN_PYTHON

PYTHON = Path("/usr/bin/python3")


class EtapeHote:
    """Socle commun : section D/E, aucune dépendance, jamais sautée."""

    section = "D"
    requires: tuple[str, ...] = ()

    def skip_if(self, ctx) -> str | None:
        return None


# ─── E. paquets et interpréteur ──────────────────────────────────────────────


def _version_python(chemin: Path) -> tuple[int, int] | None:
    """Version de l'interpréteur, ou None s'il n'y en a pas.

    Demandée à l'interpréteur lui-même plutôt que déduite du nom du fichier :
    `/usr/bin/python3` est un lien dont la cible change d'une version de Debian
    à l'autre.
    """
    try:
        res = subprocess.run(
            [str(chemin), "-c",
             "import sys; print('%d %d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    majeur, _, mineur = res.stdout.strip().partition(" ")
    try:
        return int(majeur), int(mineur)
    except ValueError:
        return None


class Python3Hote(EtapeHote):
    """`python3` vient du template Debian, pas d'une décision explicite.

    Le constater ici plutôt que de le découvrir au premier `pg offsite` de
    3h30. Aucune action n'est proposée : on ne « pose » pas une version
    d'interpréteur, et une action qui appellerait `apt` sur l'hyperviseur
    dépasserait ce que ce script a le droit de faire.
    """

    name = "python3 (hôte)"
    section = "E"

    def check(self, ctx) -> Outcome:
        version = _version_python(PYTHON)
        if version is None:
            return Outcome("error", f"absent ou muet : {PYTHON}")
        lisible = ".".join(str(n) for n in version)
        if version < MIN_PYTHON:
            attendu = ".".join(str(n) for n in MIN_PYTHON)
            return Outcome(
                "error",
                f"{lisible} sur {PYTHON} — {attendu} minimum requis par pg",
            )
        return Outcome("ok", f"{lisible} ({PYTHON})")


class Rclone(EtapeHote):
    """Le binaire de la copie hors-site, à l'emplacement que déclare l'unité.

    Le chemin n'est pas deviné : il vient de `Environment=PGBK_OFFSITE_RCLONE`,
    et l'unité du dépôt reste la source de vérité. Si le paquet s'installe
    ailleurs, mieux vaut l'apprendre ici qu'à 3h30.
    """

    name = "rclone"
    section = "E"

    def __init__(self, binaire: Path) -> None:
        self.binaire = Path(binaire)

    def skip_if(self, ctx) -> str | None:
        if not ctx.opts.do_offsite:
            return "--no-offsite : rclone n'est une dépendance que du hors-site"
        return None

    def check(self, ctx) -> Outcome:
        import os

        if os.access(self.binaire, os.X_OK):
            return Outcome("ok", str(self.binaire))
        if not ctx.opts.do_install:
            return Outcome(
                "error",
                f"absent de {self.binaire} et --no-install — "
                "la copie hors-site ne sera pas armée",
            )
        return Outcome(
            "absent",
            f"absent de {self.binaire}",
            (
                Action("apt-get update -qq",
                       lambda c: c.runner.write("apt-get", "update", "-qq")),
                Action("apt-get install -y -qq rclone",
                       lambda c: c.runner.write(
                           "env", "DEBIAN_FRONTEND=noninteractive",
                           "apt-get", "install", "-y", "-qq", "rclone")),
            ),
        )


# ─── D. outillage du nœud ────────────────────────────────────────────────────


def _conforme(source: Path, cible: Path, mode: int) -> bool:
    """Même contenu ET même mode. Sans effet de bord : c'est un `check()`."""
    if not cible.is_file():
        return False
    if cible.read_bytes() != source.read_bytes():
        return False
    return (cible.stat().st_mode & 0o777) == mode


def _poser(source: Path, cible: Path, mode: int) -> Action:
    return Action(
        f"install -m {mode:o} {source} {cible}",
        lambda c, s=source, d=cible, m=mode: c.fs.install(s, d, mode=m),
    )


class FichierHote(EtapeHote):
    """Une copie, pas un lien.

    Le dépôt peut être en cours de `git pull` à l'heure où un timer se
    déclenche : un exécutable à moitié réécrit ne pardonne pas. C'est aussi
    pourquoi le montage, lui, ne peut pas porter le bit d'exécution.
    """

    mode = 0o755

    def source(self, ctx) -> Path:
        raise NotImplementedError

    def cible(self, ctx) -> Path:
        raise NotImplementedError

    def check(self, ctx) -> Outcome:
        source, cible = self.source(ctx), self.cible(ctx)
        if _conforme(source, cible, self.mode):
            return Outcome("ok", str(cible))
        etat = "drift" if cible.exists() else "absent"
        return Outcome(etat, str(cible), (_poser(source, cible, self.mode),))


class PgbkHote(FichierHote):
    """Lu dans `ct/` : `pgbk.sh` est la charge utile du montage ET le point
    d'entrée de l'hôte. Un seul fichier, deux rôles."""

    name = "pgbk (hôte)"

    def source(self, ctx) -> Path:
        return ctx.paths.ct_src / "pgbk.sh"

    def cible(self, ctx) -> Path:
        return ctx.paths.host_pgbk


class PgHote(FichierHote):
    """Le lanceur : seul fichier exécutable de l'ensemble, et le seul du PATH."""

    name = "pg (hôte)"

    def source(self, ctx) -> Path:
        return ctx.paths.launcher

    def cible(self, ctx) -> Path:
        return ctx.paths.host_pg


class PgtoolHote(EtapeHote):
    """L'arbre d'import de `pg` sur le nœud : `core`, `proxmox`, `pgtool`.

    Le nœud reçoit `proxmox`, contrairement au conteneur : c'est lui qui parle
    à `pct`.

    Ce qui n'est plus dans le dépôt est RETIRÉ. Sans élagage, un module renommé
    laisse son ancêtre en place, et cet ancêtre continue de s'importer — le
    nœud tournerait sur du code que le dépôt ne contient plus.
    """

    name = "arbre d'import (hôte)"
    section = "D"
    requires = ("python3 (hôte)",)

    def skip_if(self, ctx) -> str | None:
        return None

    def _sources(self, ctx) -> dict[str, Path]:
        """Chemin relatif → fichier source, pour les trois paquets."""
        racines = [
            ctx.paths.lib_src / "core",
            ctx.paths.lib_src / "proxmox",
            ctx.paths.pgtool_src,
        ]
        trouves: dict[str, Path] = {}
        for racine in racines:
            if not racine.is_dir():
                continue
            for fichier in sorted(racine.rglob("*.py")):
                rel = f"{racine.name}/{fichier.relative_to(racine)}"
                trouves[rel] = fichier
        return trouves

    def _empreintes(self, fichiers) -> dict[str, str]:
        return {
            rel: hashlib.sha256(chemin.read_bytes()).hexdigest()
            for rel, chemin in fichiers.items()
        }

    def _poses(self, racine: Path) -> dict[str, str]:
        if not racine.is_dir():
            return {}
        return {
            str(chemin.relative_to(racine)):
                hashlib.sha256(chemin.read_bytes()).hexdigest()
            for chemin in sorted(racine.rglob("*.py"))
        }

    def check(self, ctx) -> Outcome:
        sources = self._sources(ctx)
        racine = ctx.paths.host_lib
        a_poser, a_retirer = diff_tree(self._empreintes(sources),
                                       self._poses(racine))
        if not a_poser and not a_retirer:
            return Outcome("ok", f"{racine} — {len(sources)} module(s)")

        actions = [
            _poser(sources[rel], racine / rel, 0o644) for rel in a_poser
        ]
        actions += [
            Action(
                f"rm {racine / rel} (absent du dépôt)",
                lambda c, chemin=racine / rel: c.fs.remove(chemin),
            )
            for rel in a_retirer
        ]
        etat = "absent" if not racine.is_dir() else "drift"
        detail = f"{len(a_poser)} à poser, {len(a_retirer)} à retirer"
        return Outcome(etat, detail, tuple(actions))


class ConfCtid(EtapeHote):
    """`/etc/default/pgbk` — la source unique du CTID.

    Sans ce fichier, `pgbk` s'arrête plutôt que de taper dans un conteneur
    supposé. Il se règle en rejouant `pg deploy --ctid <ID>`, jamais en
    l'éditant : c'est ce que dit son en-tête, et le déploiement le fait tenir.
    """

    name = "CTID consigné"

    def _contenu(self, ctx) -> str:
        return "\n".join((
            "# Généré par pg deploy — conteneur PostgreSQL piloté par pg.",
            "# Changer de CT : rejouer « pg deploy --ctid <ID> », "
            "pas éditer ce fichier.",
            f"PG_CTID={ctx.opts.ctid}",
        )) + "\n"

    def check(self, ctx) -> Outcome:
        cible = ctx.paths.conf
        voulu = self._contenu(ctx)
        actuel = cible.read_text() if cible.is_file() else ""
        if actuel == voulu:
            return Outcome("ok", f"{cible} : PG_CTID={ctx.opts.ctid}")
        return Outcome(
            "drift" if actuel else "absent",
            f"{cible} — PG_CTID={ctx.opts.ctid}",
            (
                Action(
                    f"écrire {cible} (PG_CTID={ctx.opts.ctid})",
                    lambda c, p=cible, t=voulu: c.fs.write_file(p, t, mode=0o644),
                ),
            ),
        )
