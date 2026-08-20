"""Proxmox — opérations de nœud et de conteneur, indépendantes du service.

Ce module ne quitte JAMAIS l'hôte : un conteneur n'a rien à faire avec `pct`.
C'est aussi ce qui confine le verrouillage Proxmox à un seul endroit — le jour
où un service migre vers une VM ou une machine nue, seul ce fichier est à
remplacer.

Rien ici ne mentionne PostgreSQL, Forgejo ni aucun service. La règle est
simple : si un nom de service apparaît, le code est au mauvais endroit.

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

import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from ..core.log import info, step, warn
from ..core.runner import Runner

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
        """Compare sans dépendre de l'ordre des options."""
        if not current:
            return False
        return set(current.split(",")) == set(self.render().split(","))


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

    def push_tree(self, src: Path, dst: str, *, perms: str = "0644") -> int:
        """Copie un répertoire fichier par fichier — `pct push` ne fait qu'un
        fichier à la fois. Sert à déposer la bibliothèque core dans le CT."""
        self.exec("mkdir", "-p", dst)
        count = 0
        for path in sorted(src.rglob("*")):
            if path.is_dir():
                self.exec("mkdir", "-p", f"{dst}/{path.relative_to(src)}")
                continue
            self.push(path, f"{dst}/{path.relative_to(src)}", perms=perms)
            count += 1
        return count

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

        Un `mpN` n'est pris en compte qu'au démarrage : poser sans redémarrer
        donne un répertoire vide côté conteneur, sans message d'erreur.
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


class Storage:
    """`pvesm` — les stockages déclarés au niveau du nœud."""

    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    def status(self) -> dict[str, StorageInfo]:
        out: dict[str, StorageInfo] = {}
        for line in self.runner.read("pvesm", "status").lines[1:]:
            name, kind, state, total, used, avail, *_ = line.split()
            out[name] = StorageInfo(
                name, kind, state == "active", int(total), int(used), int(avail)
            )
        return out

    def exists(self, name: str) -> bool:
        return name in self.status()

    def add_zfspool(self, name: str, pool: str, *, content: str = "images,rootdir") -> None:
        self.runner.write(
            "pvesm", "add", "zfspool", name, "--pool", pool, "--content", content
        )


# ─── ZFS ─────────────────────────────────────────────────────────────────────


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
        out: dict[str, str] = {}
        for line in self.runner.read("zfs", "list", "-H", "-o", "name,mountpoint").lines:
            name, _, mountpoint = line.partition("\t")
            out[name] = mountpoint.strip()
        return out

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
        return self.runner.read("hostname").out

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