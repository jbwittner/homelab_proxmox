"""Ce que les étapes de déploiement partagent : chemins, drapeaux, outils.

`fj deploy` tourne **sur le nœud, jamais dans le conteneur** — il lui faut
`pct`. Ce module et tout `fjtool/steps/` sont donc du code d'hôte, même s'ils
voyagent avec le reste du paquet ; le conteneur ne les importe simplement pas,
ce que garantissent les imports paresseux de `cli`.

Le découpage est celui de `pgtool` — et volontairement : deux services du même
dépôt qui se déploient de deux façons différentes coûtent deux apprentissages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.converge import Context, Mode
from core.runner import Fs, Runner

# ─── Chemins d'installation sur le NŒUD ──────────────────────────────────────
# Absolus : le PATH de systemd et de `pct exec` est minimal et n'inclut ni
# /usr/local/bin ni /usr/local/sbin.
HOST_FJ = Path("/usr/local/sbin/fj")
HOST_LIB = Path("/usr/local/lib/fjtool")
CONF = Path("/etc/default/fjbk")

# ─── Chemins DANS le conteneur ───────────────────────────────────────────────
CT_LIB = Path("/usr/local/lib/fjtool")
CT_FJ = Path("/usr/local/bin/fj")

# Le contrat du montage. Ce chemin ne bouge jamais : des unités systemd, des
# symlinks de configuration et la documentation le nomment.
MP = "/etc/forgejo-git"

# L'installation binaire. `/opt/forgejo/forgejo` est le fichier réellement
# lancé par l'unité ; le symlink n'est là que pour la main humaine.
CT_OPT = "/opt/forgejo"
CT_BINAIRE = "/opt/forgejo/forgejo"
CT_SYMLINK = "/usr/local/bin/forgejo"

# La configuration et les secrets, côté conteneur.
CT_ETC = "/etc/forgejo"
CT_APP_INI = "/etc/forgejo/app.ini"
CT_SECRETS = "/etc/forgejo/secrets"

# Les données : dépôts, LFS, pièces jointes, dépôt de sessions.
CT_DATA = "/var/lib/forgejo"

# Les quatre secrets que Forgejo GÉNÈRERAIT lui-même s'ils manquaient — en
# réécrivant app.ini au passage. Les pré-déposer est ce qui rend une
# configuration versionnée tenable : voir doc/RUNBOOK.md section 7.
#
# Le nom de fichier correspond à la clé `*_URI` d'app.ini qui le désigne.
SECRETS = ("secret_key", "internal_token", "oauth2_jwt_secret", "lfs_jwt_secret")


@dataclass
class Paths:
    """Où lire, et où poser.

    `src` est la racine du service. `ct/` en est la charge utile du montage et
    `host/` ce qui s'installe sur le nœud ; `lib/` vit un cran plus haut,
    partagé par les deux nœuds.
    """

    src: Path
    host_fj: Path = HOST_FJ
    host_lib: Path = HOST_LIB
    ct_lib: Path = CT_LIB
    ct_fj: Path = CT_FJ
    conf: Path = CONF

    @property
    def ct_src(self) -> Path:
        return self.src / "ct"

    @property
    def host_src(self) -> Path:
        return self.src / "host"

    @property
    def fjtool_src(self) -> Path:
        return self.src / "fjtool"

    @property
    def lib_src(self) -> Path:
        """`lib/` est à la racine du dépôt, deux crans au-dessus du service."""
        return self.src.parents[1] / "lib"

    @property
    def launcher(self) -> Path:
        return self.src / "fj"

    @property
    def version_file(self) -> Path:
        """Le fichier VERSION est dans `ct/` : le conteneur doit pouvoir le
        lire, c'est ce qui permet de comparer l'épinglage au binaire posé."""
        return self.ct_src / "VERSION"


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

    # Le volume des sauvegardes, sur un disque DISTINCT de celui des dépôts.
    # La taille ne sert qu'à la CRÉATION : après, c'est Proxmox qui décide du
    # nom du volume, et l'agrandir est un geste séparé (`pct resize`).
    mp2_mount: str = "/var/backups/forgejo"
    mp2_storage: str = "data"
    mp2_size: int = 20


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
