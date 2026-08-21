"""Sections D/E — l'outillage du nœud.

Ce que le nœud porte, et que le conteneur ne voit pas : le lanceur `fj`, son
arbre d'import (`core`, `proxmox`, `fjtool`), le CTID consigné, `rclone` et
`gnupg`.

`gnupg` est ici et non dans le conteneur, et c'est la conséquence directe du
choix de télécharger sur le nœud : ce qui n'a pas été vérifié ne doit jamais
toucher le disque du conteneur, donc le trousseau et le vérificateur vivent du
côté qui télécharge.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from core import MIN_PYTHON
from core.converge import Action, Outcome
from proxmox import Node, diff_tree

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
    if res.returncode != 0 or not res.stdout.split():
        return None
    majeur, mineur = res.stdout.split()[:2]
    return (int(majeur), int(mineur))


class Python3Hote(EtapeHote):
    """L'interpréteur du nœud, et sa version.

    Le contrôle porte sur la MÊME borne que `core.require_python` : deux seuils
    différents donneraient un déploiement vert et un « fj » qui refuse de
    tourner.
    """

    name = "python3 (hôte)"

    def check(self, ctx) -> Outcome:
        trouvee = _version_python(PYTHON)
        if trouvee is None:
            return Outcome(
                "error",
                f"{PYTHON} absent ou inutilisable — le moteur ne tournera pas",
            )
        if trouvee < MIN_PYTHON:
            attendu = ".".join(str(n) for n in MIN_PYTHON)
            vue = ".".join(str(n) for n in trouvee)
            return Outcome(
                "error", f"python3 {vue} trouvé, {attendu} minimum requis"
            )
        return Outcome("ok", ".".join(str(n) for n in trouvee))


class PaquetHote(EtapeHote):
    """Un paquet du nœud, constaté par son binaire.

    Proxmox est une Debian : `apt-get` y est légitime. `--no-install` ne
    désactive que la pose, jamais le constat.
    """

    section = "E"

    def __init__(self, paquet: str, binaire: Path, pourquoi: str) -> None:
        self.paquet = paquet
        self.binaire = binaire
        self.pourquoi = pourquoi
        self.name = paquet

    def check(self, ctx) -> Outcome:
        # Le fait est posé AVANT toute sortie, y compris en échec : c'est lui
        # que l'armement du hors-site consulte, et un fait absent y vaut refus.
        # L'oublier sur une branche d'échec ferait armer un timer sur un
        # `rclone` inexistant — la panne se lirait à 3h50, pas ici.
        present = self.binaire.is_file()
        ctx.facts[f"{self.paquet}_ok"] = present

        if present:
            return Outcome("ok", str(self.binaire))
        if not ctx.opts.do_install:
            return Outcome(
                "error", f"{self.paquet} absent et --no-install — {self.pourquoi}"
            )
        return Outcome(
            "absent",
            f"{self.binaire} absent — {self.pourquoi}",
            (
                Action(
                    f"apt-get install -y -qq {self.paquet}",
                    lambda c, p=self.paquet: Node(c.runner).ensure_packages(p),
                ),
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


class FjHote(EtapeHote):
    """Le lanceur du nœud — une COPIE, pas un lien.

    Le dépôt peut être en cours de `git pull` à l'heure où le timer hors-site
    se déclenche : un exécutable à moitié réécrit ne pardonne pas.
    """

    name = "fj (hôte)"
    mode = 0o755

    def check(self, ctx) -> Outcome:
        source, cible = ctx.paths.launcher, ctx.paths.host_fj
        if _conforme(source, cible, self.mode):
            return Outcome("ok", str(cible))
        etat = "drift" if cible.exists() else "absent"
        return Outcome(etat, str(cible), (_poser(source, cible, self.mode),))


class FjtoolHote(EtapeHote):
    """L'arbre d'import de `fj` sur le nœud : `core`, `proxmox`, `fjtool`.

    Le nœud reçoit `proxmox`, contrairement au conteneur : c'est lui qui parle
    à `pct`.

    Ce qui n'est plus dans le dépôt est RETIRÉ. Sans élagage, un module renommé
    laisse son ancêtre en place, et cet ancêtre continue de s'importer — le
    nœud tournerait sur du code que le dépôt ne contient plus.
    """

    name = "arbre d'import (hôte)"
    requires = ("python3 (hôte)",)

    def _sources(self, ctx) -> dict[str, Path]:
        racines = [
            ctx.paths.lib_src / "core",
            ctx.paths.lib_src / "proxmox",
            ctx.paths.fjtool_src,
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
        a_poser, a_retirer = diff_tree(
            self._empreintes(sources), self._poses(racine)
        )
        if not a_poser and not a_retirer:
            return Outcome("ok", f"{racine} — {len(sources)} module(s)")

        actions = [_poser(sources[rel], racine / rel, 0o644) for rel in a_poser]
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
    """`/etc/default/fjbk` — la source unique du CTID.

    Sans ce fichier, `fj` s'arrête plutôt que de taper dans un conteneur
    supposé. Il se règle en rejouant `fj deploy --ctid <ID>`, jamais en
    l'éditant : c'est ce que dit son en-tête, et le déploiement le fait tenir.
    """

    name = "CTID consigné"

    def _contenu(self, ctx) -> str:
        return "\n".join((
            "# Généré par fj deploy — conteneur Forgejo piloté par fj.",
            "# Changer de CT : rejouer « fj deploy --ctid <ID> », "
            "pas éditer ce fichier.",
            f"FJ_CTID={ctx.opts.ctid}",
        )) + "\n"

    def check(self, ctx) -> Outcome:
        cible = ctx.paths.conf
        voulu = self._contenu(ctx)
        actuel = cible.read_text() if cible.is_file() else ""
        if actuel == voulu:
            return Outcome("ok", f"{cible} : FJ_CTID={ctx.opts.ctid}")
        return Outcome(
            "drift" if actuel else "absent",
            f"{cible} — FJ_CTID={ctx.opts.ctid}",
            (
                Action(
                    f"écrire {cible} (FJ_CTID={ctx.opts.ctid})",
                    lambda c, p=cible, t=voulu: c.fs.write_file(p, t, mode=0o644),
                ),
            ),
        )
