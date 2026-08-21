"""Ce que les étapes de déploiement partagent : chemins, drapeaux, outils.

`pg deploy` tourne **sur le nœud, jamais dans le conteneur** — il lui faut
`pct`. Ce module et tout `pgtool/steps/` sont donc du code d'hôte, même s'ils
voyagent avec le reste du paquet ; le conteneur ne les importe simplement pas,
ce que garantissent les imports paresseux de `cli`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.converge import Context, Mode
from core.runner import Fs, Runner

# Chemins d'installation sur le nœud. Absolus : le PATH de systemd et de
# `pct exec` est minimal et n'inclut ni /usr/local/bin ni /usr/local/sbin.
HOST_PGBK = Path("/usr/local/sbin/pgbk")
HOST_PG = Path("/usr/local/sbin/pg")
HOST_LIB = Path("/usr/local/lib/pgtool")
CT_LIB = Path("/usr/local/lib/pgtool")
CT_PG = Path("/usr/local/bin/pg")
CONF = Path("/etc/default/pgbk")
MP = "/etc/pgsql-git"


@dataclass
class Paths:
    """Où lire, et où poser.

    `src` est la racine du service. `ct/` en est la charge utile du montage et
    `host/` ce qui s'installe sur le nœud ; `lib/` vit un cran plus haut,
    partagé par les deux nœuds.
    """

    src: Path
    host_pgbk: Path = HOST_PGBK
    host_pg: Path = HOST_PG
    host_lib: Path = HOST_LIB
    ct_lib: Path = CT_LIB
    ct_pg: Path = CT_PG
    conf: Path = CONF

    @property
    def ct_src(self) -> Path:
        return self.src / "ct"

    @property
    def host_src(self) -> Path:
        return self.src / "host"

    @property
    def pgtool_src(self) -> Path:
        return self.src / "pgtool"

    @property
    def lib_src(self) -> Path:
        """`lib/` est à la racine du dépôt, deux crans au-dessus du service."""
        return self.src.parents[1] / "lib"

    @property
    def launcher(self) -> Path:
        return self.src / "pg"


@dataclass(frozen=True)
class Options:
    """Les drapeaux de la ligne de commande.

    Ils ne désactivent jamais un contrôle, seulement une POSE : `--no-install`
    n'empêche pas de constater qu'un paquet manque, il empêche de l'installer.
    C'est ce qui permet à `--status` de rester complet quels que soient les
    drapeaux.
    """

    ctid: int
    do_container: bool = True
    do_offsite: bool = True
    do_install: bool = True
    do_first_run: bool = True
    force_restart: bool = False
    admin: str | None = None
    tenant: str | None = None

    # Le volume des sauvegardes. La taille ne sert qu'à la CRÉATION : après,
    # c'est Proxmox qui décide du nom du volume, et l'agrandir est un geste
    # séparé (`pct resize`).
    mp2_mount: str = "/var/backups/postgresql"
    mp2_storage: str = "data"
    mp2_size: int = 50


@dataclass
class DeployContext(Context):
    """Le contexte générique, plus ce qui est propre à ce déploiement."""

    runner: Runner = field(default_factory=Runner)
    fs: Fs = field(default_factory=Fs)
    paths: Paths | None = None
    opts: Options | None = None


def contexte(
    *,
    runner: Runner,
    paths: Paths,
    opts: Options,
    mode: Mode = Mode.APPLY,
    allow_secrets: bool = False,
) -> DeployContext:
    """Fabrique un contexte cohérent.

    En simulation, le Runner est mis en mode `dry_run` : une écriture égarée
    dans un `check()` mal écrit y est neutralisée plutôt qu'exécutée. Le filet
    n'excuse pas le défaut, mais il évite qu'il coûte quelque chose.
    """
    simulation = not mode.applies
    runner.dry_run = simulation
    return DeployContext(
        mode=mode,
        allow_secrets=allow_secrets,
        runner=runner,
        fs=Fs(dry_run=simulation),
        paths=paths,
        opts=opts,
    )
