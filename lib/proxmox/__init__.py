"""Proxmox — opérations de nœud et de conteneur, indépendantes du service.

Ce module ne quitte JAMAIS l'hôte : un conteneur n'a rien à faire avec `pct`.
C'est aussi ce qui confine le verrouillage Proxmox à un seul endroit — le jour
où un service migre vers une VM ou une machine nue, seul ce fichier est à
remplacer.

Rien ici ne nomme un service particulier. La règle est simple : si le nom d'un
service du homelab apparaît, le code est au mauvais endroit — et un test le
vérifie, sans exception, y compris dans les commentaires.

Les pièges encodés ici viennent tous de pannes réelles :
  - la protection interdit l'ajout d'un point de montage, et l'oublier au
    retour ne produit aucune erreur ;
  - un point de montage n'est pris en compte qu'au DÉMARRAGE du conteneur ;
  - nesting=1 est obligatoire sur Debian 13, sans quoi les unités qui
    utilisent PrivateTmp échouent en 243/CREDENTIALS et le conteneur démarre
    en état dégradé, sans que rien ne le signale ;
  - le recordsize ZFS ne s'applique qu'aux blocs neufs : après les données,
    il est trop tard.
"""

from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from core.log import info, step, warn
from core.runner import Runner

WAIT_TIMEOUT = 120


class ProxmoxError(RuntimeError):
    pass


# ─── Conteneur ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MountPoint:
    """Un `mpN`. Deux natures très différentes derrière la même option.

    bind    : `/chemin/hôte,mp=...` — un répertoire existant de l'hôte, pour
              de la configuration versionnée. `ro=1` par défaut : le conteneur
              consomme, il n'écrit pas.
    volume  : `<storage>:<Go>,mp=...` — un volume neuf, vide, formaté. Pour
              des données propres au conteneur.
    """

    index: int
    source: str
    target: str
    readonly: bool = False
    backup: bool | None = None

    @property
    def key(self) -> str:
        return f"mp{self.index}"

    def render(self) -> str:
        parts = [self.source, f"mp={self.target}"]
        if self.readonly:
            parts.append("ro=1")
        if self.backup is not None:
            parts.append(f"backup={int(self.backup)}")
        return ",".join(parts)

    def matches(self, current: str | None) -> bool:
        """Compare sans dépendre de l'ordre des options.

        Proxmox réécrit la valeur qu'on lui donne : l'ordre des options n'est
        pas garanti, et une comparaison de chaînes brutes conclurait à une
        divergence à chaque déploiement — donc à un point de montage reposé et
        à un conteneur redémarré pour rien.
        """
        if not current:
            return False
        return set(current.split(",")) == set(self.render().split(","))


@dataclass(frozen=True)
class TreeChange:
    """Ce qu'une synchronisation de répertoire a fait, ou ferait."""

    pushed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    unchanged: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.pushed or self.removed)


def diff_tree(
    local: dict[str, str], remote: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Décide quoi pousser et quoi retirer, à partir de deux tables d'empreintes.

    Fonction pure, donc testable sans conteneur — et c'est là qu'est toute la
    décision.

    L'élagage n'est pas un raffinement : sans lui, un module renommé laisse son
    ancêtre en place, et cet ancêtre continue de s'importer. Le conteneur
    tournerait alors sur du code que le dépôt ne contient plus.
    """
    to_push = sorted(k for k, digest in local.items() if remote.get(k) != digest)
    to_remove = sorted(k for k in remote if k not in local)
    return to_push, to_remove


class Container:
    """Un CT vu depuis le nœud.

    `exec()` transmet un argv à `pct exec`, qui ne l'interprète pas : aucun
    échappement, et le code de retour distant devient celui de la commande.
    """

    def __init__(self, runner: Runner, ctid: int) -> None:
        self.runner = runner
        self.ctid = ctid

    def __repr__(self) -> str:
        return f"Container({self.ctid})"

    # -- état ---------------------------------------------------------------

    def exists(self) -> bool:
        return self.runner.probe("pct", "config", str(self.ctid))

    @property
    def status(self) -> str:
        return self.runner.read("pct", "status", str(self.ctid)).out.split()[-1]

    @property
    def running(self) -> bool:
        return self.status == "running"

    def config(self) -> dict[str, str]:
        """`pct config` en dictionnaire.

        Découpe sur le PREMIER deux-points seulement : la valeur d'un point de
        montage en contient un elle-même (`data:subvol-200-disk-0`), qu'un
        séparateur trop gourmand couperait en plein milieu.
        """
        conf: dict[str, str] = {}
        for line in self.runner.read("pct", "config", str(self.ctid)).lines:
            key, _, value = line.partition(":")
            conf[key.strip()] = value.strip()
        return conf

    def features(self) -> set[str]:
        return set(filter(None, self.config().get("features", "").split(",")))

    # -- actions ------------------------------------------------------------

    def set(self, **options: object) -> None:
        argv = ["pct", "set", str(self.ctid)]
        for key, value in options.items():
            argv += [f"--{key}", str(value)]
        self.runner.write(*argv)

    def start(self) -> None:
        self.runner.write("pct", "start", str(self.ctid))

    def reboot(self) -> None:
        self.runner.write("pct", "reboot", str(self.ctid))

    def exec(self, *argv: str, check: bool = True):
        return self.runner.read(
            "pct", "exec", str(self.ctid), "--", *argv, check=check
        )

    def push(self, src: Path, dst: str, *, perms: str = "0644") -> None:
        self.runner.write(
            "pct", "push", str(self.ctid), str(src), dst, "--perms", perms
        )

    # -- dépôt d'un arbre de fichiers ---------------------------------------

    def digests(self, root: str) -> dict[str, str]:
        """Empreintes des fichiers présents sous `root` dans le CT.

        Un seul aller-retour plutôt qu'un `cmp` par fichier. Le script shell
        est une CONSTANTE et le chemin arrive en argument : rien n'est
        concaténé, donc rien n'est interprétable.
        """
        res = self.exec(
            "sh",
            "-c",
            'cd "$1" 2>/dev/null && find . -type f -exec sha256sum {} + || true',
            "sh",
            root,
            check=False,
        )
        out: dict[str, str] = {}
        for line in res.lines:
            digest, _, path = line.partition("  ")
            if path.startswith("./"):
                out[path[2:]] = digest
        return out

    def push_tree(
        self, src: Path, dst: str, *, perms: str = "0644"
    ) -> TreeChange:
        """Synchronise un répertoire vers le CT. `pct push` ne fait qu'un
        fichier à la fois, d'où la boucle.

        Ne pousse que ce qui diffère, et retire ce que le dépôt ne contient
        plus. Pousser inconditionnellement ferait annoncer des modifications
        par `--dry-run` sur un conteneur conforme — or « zéro modification sur
        un état conforme » est le contrôle qui prouve que l'outil décrit l'état
        existant et non un état voisin.
        """
        local = {
            str(p.relative_to(src)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(src.rglob("*"))
            if p.is_file()
        }
        remote = self.digests(dst)
        to_push, to_remove = diff_tree(local, remote)

        if to_push or to_remove:
            self.exec("mkdir", "-p", dst)
        for rel in to_push:
            parent = f"{dst}/{rel}".rsplit("/", 1)[0]
            if parent != dst:
                self.exec("mkdir", "-p", parent)
            self.push(src / rel, f"{dst}/{rel}", perms=perms)
        for rel in to_remove:
            self.runner.write("pct", "exec", str(self.ctid), "--", "rm", "-f",
                              f"{dst}/{rel}")

        return TreeChange(
            pushed=tuple(to_push),
            removed=tuple(to_remove),
            unchanged=len(local) - len(to_push),
        )

    # -- protection ---------------------------------------------------------

    @contextmanager
    def unprotected(self) -> Iterator[None]:
        """Lève la protection et la REMET, y compris sur exception.

        La protection interdit toute modification de disque, ajout de point de
        montage compris. Ne pas la remettre ne produit aucune erreur et ne se
        voit pas : d'où le `finally`.
        """
        was_protected = self.config().get("protection") == "1"
        if not was_protected:
            yield
            return
        info(f"  levée temporaire de la protection du CT {self.ctid}")
        self.set(protection=0)
        try:
            yield
        finally:
            if self.config().get("protection") != "1":
                self.set(protection=1)
                info(f"  protection du CT {self.ctid} rétablie")

    # -- points de montage --------------------------------------------------

    def ensure_mount(self, mp: MountPoint) -> bool:
        """Pose un point de montage s'il diffère. True si le CT doit redémarrer.

        Le booléen renvoyé n'est pas une commodité : un `mpN` n'est pris en
        compte qu'au démarrage, et poser sans redémarrer donne un répertoire
        vide côté conteneur, sans le moindre message d'erreur. L'appelant ne
        peut pas ignorer l'information — elle est dans la valeur de retour.
        """
        if mp.matches(self.config().get(mp.key)):
            return False
        with self.unprotected():
            self.set(**{mp.key: mp.render()})
        return True

    def ensure_feature(self, feature: str) -> bool:
        """Ajoute une feature en préservant les autres. True si modifié.

        `nesting=1` est obligatoire sur Debian 13 : sans lui, les unités qui
        montent un tmpfs pour les credentials systemd échouent en
        243/CREDENTIALS.
        """
        current = self.features()
        name = feature.split("=")[0]
        if feature in current:
            return False
        kept = {f for f in current if not f.startswith(f"{name}=")}
        self.set(features=",".join(sorted(kept | {feature})))
        return True

    # -- attente ------------------------------------------------------------

    def wait_until(
        self, label: str, predicate: Callable[[], bool], *, timeout: int = WAIT_TIMEOUT
    ) -> None:
        info(f"  attente : {label}")
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise ProxmoxError(f"délai dépassé ({timeout}s) : {label}")
            time.sleep(3)

    def wait_running(self, *, timeout: int = WAIT_TIMEOUT) -> None:
        self.wait_until(f"CT {self.ctid} démarré", lambda: self.running, timeout=timeout)

    def wait_unit(self, unit: str, *, timeout: int = WAIT_TIMEOUT) -> None:
        self.wait_until(
            f"{unit} actif dans le CT {self.ctid}",
            lambda: self.exec(
                "systemctl", "is-active", "--quiet", unit, check=False
            ).ok,
            timeout=timeout,
        )

    def has_binary(self, name: str) -> bool:
        """Le PATH de `pct exec` est minimal : /usr/local/bin n'y est pas."""
        return self.exec("test", "-x", name, check=False).ok


# ─── Stockage ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StorageInfo:
    name: str
    kind: str
    active: bool
    total_kib: int
    used_kib: int
    avail_kib: int


def parse_storage_status(lines: list[str]) -> dict[str, StorageInfo]:
    """Analyse la sortie de `pvesm status`, en-tête compris.

    L'en-tête est reconnu à son contenu et non à sa position : un `pvesm` qui
    n'en émettrait pas ferait sinon disparaître le premier stockage réel.
    """
    out: dict[str, StorageInfo] = {}
    for line in lines:
        fields = line.split()
        if len(fields) < 6 or fields[0] == "Name":
            continue
        name, kind, state, total, used, avail = fields[:6]
        try:
            out[name] = StorageInfo(
                name, kind, state == "active", int(total), int(used), int(avail)
            )
        except ValueError:
            continue
    return out


class Storage:
    """`pvesm` — les stockages déclarés au niveau du nœud."""

    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    def status(self) -> dict[str, StorageInfo]:
        return parse_storage_status(self.runner.read("pvesm", "status").lines)

    def exists(self, name: str) -> bool:
        return name in self.status()

    def path(self, volid: str) -> str:
        """Chemin HÔTE d'un volume, demandé à Proxmox et non deviné.

        Un volume de conteneur porte un identifiant (`data:subvol-200-disk-0`)
        dont le chemin dépend du stockage : le déduire à la main marche jusqu'au
        jour où le pool change de nom.
        """
        return self.runner.read("pvesm", "path", volid).out

    def add_zfspool(self, name: str, pool: str, *, content: str = "images,rootdir") -> None:
        self.runner.write(
            "pvesm", "add", "zfspool", name, "--pool", pool, "--content", content
        )


# ─── ZFS ─────────────────────────────────────────────────────────────────────


def parse_zfs_list(lines: list[str]) -> dict[str, str]:
    """`zfs list -H -o name,mountpoint` : nom → point de montage.

    `-H` sépare par une TABULATION. Un découpage sur les espaces casserait sur
    un point de montage qui en contient un.
    """
    out: dict[str, str] = {}
    for line in lines:
        name, _, mountpoint = line.partition("\t")
        if name:
            out[name.strip()] = mountpoint.strip()
    return out


class Zfs:
    """Pools et datasets.

    Deux réglages sont DÉFINITIFS et doivent être posés avant les données :
      - `ashift`, fixé à la création du pool, jamais modifiable ;
      - `recordsize`, modifiable mais sans effet rétroactif — il ne s'applique
        qu'aux blocs nouvellement écrits.
    """

    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    def available(self) -> bool:
        return self.runner.which("zpool") is not None

    def pool_exists(self, pool: str) -> bool:
        return self.runner.probe("zpool", "list", pool)

    def pool_healthy(self, pool: str) -> bool:
        res = self.runner.read("zpool", "list", "-H", "-o", "health", pool, check=False)
        return res.out == "ONLINE"

    def create_pool(self, pool: str, device: str, *, ashift: int = 12) -> None:
        """`device` doit être un chemin /dev/disk/by-id/ : les noms /dev/nvmeXnY
        peuvent permuter au redémarrage, et un pool qui ne retrouve pas son
        disque au boot coûte une matinée."""
        if not device.startswith("/dev/disk/by-id/"):
            raise ProxmoxError(f"chemin instable, utiliser /dev/disk/by-id/ : {device}")
        self.runner.write(
            "zpool", "create",
            "-o", f"ashift={ashift}",
            "-O", "compression=lz4",
            "-O", "atime=off",
            pool, device,
        )

    def datasets(self) -> dict[str, str]:
        """nom → point de montage."""
        return parse_zfs_list(
            self.runner.read("zfs", "list", "-H", "-o", "name,mountpoint").lines
        )

    def container_dataset(self, ctid: int, disk: int = 0) -> str | None:
        """Chemin HÔTE du volume d'un conteneur.

        Proxmox crée des datasets (`subvol-<CTID>-disk-N`) pour les points de
        montage de conteneur, pas des zvols : le contenu est donc lisible
        directement depuis le nœud. C'est ce qui permet à un outil hôte de
        traiter les fichiers d'un CT sans y entrer.
        """
        suffix = f"subvol-{ctid}-disk-{disk}"
        for name, mountpoint in self.datasets().items():
            if name.endswith(suffix):
                return mountpoint
        return None

    def get(self, dataset: str, prop: str) -> str:
        return self.runner.read("zfs", "get", "-H", "-o", "value", prop, dataset).out

    def set(self, dataset: str, **props: object) -> None:
        for key, value in props.items():
            self.runner.write("zfs", "set", f"{key}={value}", dataset)

    def enable_scrub(self, pool: str) -> None:
        """Sans scrub, les sommes de contrôle ne servent à rien : ZFS ne relit
        jamais les données froides de lui-même."""
        self.runner.write("systemctl", "enable", "--now", f"zfs-scrub-monthly@{pool}.timer")


# ─── Nœud ────────────────────────────────────────────────────────────────────


class Node:
    """Le nœud lui-même. Point d'entrée des autres objets."""

    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self.storage = Storage(runner)
        self.zfs = Zfs(runner)

    def is_proxmox(self) -> bool:
        return self.runner.which("pct") is not None

    @property
    def hostname(self) -> str:
        return self.runner.read("hostname", "-s").out

    def container(self, ctid: int) -> Container:
        return Container(self.runner, ctid)

    def containers(self) -> dict[int, str]:
        """CTID → nom, pour l'inventaire."""
        out: dict[int, str] = {}
        for line in self.runner.read("pct", "list").lines[1:]:
            fields = line.split()
            out[int(fields[0])] = fields[-1]
        return out

    def ensure_packages(self, *names: str) -> list[str]:
        """Installe ce qui manque. Renvoie ce qui a été installé."""
        missing = [
            name for name in names
            if not self.runner.probe("dpkg", "-s", name)
        ]
        if missing:
            step(f"installation : {', '.join(missing)}")
            self.runner.write("apt-get", "update", "-qq")
            self.runner.write("apt-get", "install", "-y", "-qq", *missing)
        return missing

    def set_startup(self, ctid: int, *, order: int, up_delay: int | None = None) -> None:
        """Ordonne le démarrage au boot du nœud. Un service qui dépend d'un
        autre conteneur doit démarrer après lui, sinon il boucle au redémarrage."""
        value = f"order={order}"
        if up_delay is not None:
            value += f",up={up_delay}"
        self.container(ctid).set(startup=value)
