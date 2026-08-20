
"""Wrappers d'outils — une classe par binaire, des méthodes plutôt que des argv.

Le reste du code n'écrit jamais `subprocess`, `pct` ni `psql` : il appelle des
méthodes qui renvoient des types Python. Chaque particularité d'un outil — le
format de `pct config`, les drapeaux `-tA` de psql, la citation des
identifiants SQL — est traitée ici, une fois, et testée une fois.

Aucune de ces classes ne sait où elle tourne : c'est le Runner qu'on leur passe
qui décide. `Psql(runner)` interroge le cluster local ; `Psql(runner.
for_container(200))` interroge celui du CT 200 depuis le nœud. Même code.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .log import info, warn
from .runner import CommandError, Result, Runner

# ─── SQL ─────────────────────────────────────────────────────────────────────


def ident(name: str) -> str:
    """Cite un identifiant SQL. Les guillemets internes se doublent."""
    return '"' + name.replace('"', '""') + '"'


class Psql:
    """Accès au cluster PostgreSQL, en peer sur socket Unix.

    Les valeurs ne sont JAMAIS interpolées dans le SQL : elles passent par les
    variables psql (-v), qui citent elles-mêmes. C'est ce qui permet un mot de
    passe contenant n'importe quel caractère, là où une substitution sed
    imposait d'interdire l'apostrophe.
    """

    def __init__(self, runner: Runner, *, user: str = "postgres") -> None:
        self.runner = runner
        self.user = user

    def _argv(self, db: str | None, extra: list[str]) -> list[str]:
        argv = ["sudo", "-u", self.user, "psql", "-v", "ON_ERROR_STOP=1", "-tA"]
        if db:
            argv += ["-d", db]
        return argv + extra

    # -- lecture ------------------------------------------------------------

    def scalar(self, sql: str, *, db: str | None = None) -> str:
        return self.runner.read(*self._argv(db, ["-c", sql])).out

    def column(self, sql: str, *, db: str | None = None) -> list[str]:
        return self.runner.read(*self._argv(db, ["-c", sql])).lines

    def rows(self, sql: str, *, db: str | None = None) -> list[list[str]]:
        res = self.runner.read(*self._argv(db, ["-F", "\x1f", "-c", sql]))
        return [ln.split("\x1f") for ln in res.lines]

    # -- écriture -----------------------------------------------------------

    def execute(self, sql: str, *, db: str | None = None) -> Result:
        return self.runner.write(*self._argv(db, ["-c", sql]))

    def run_file(self, path: str, *, db: str | None = None, **params: str) -> Result:
        """Joue un script SQL avec des variables psql.

        Correspond à `psql -v name=forgejo -v password=... -f tenant.sql`.
        """
        extra: list[str] = []
        for key, value in params.items():
            extra += ["-v", f"{key}={value}"]
        extra += ["-f", path]
        return self.runner.write(*self._argv(db, extra))

    # -- interrogations courantes -------------------------------------------

    @property
    def version(self) -> str:
        return self.scalar("SHOW server_version").split()[0]

    def setting(self, name: str) -> str:
        return self.scalar(f"SHOW {name}")

    def databases(self) -> list[str]:
        return self.column(
            "SELECT datname FROM pg_database "
            "WHERE datallowconn AND NOT datistemplate AND datname <> 'postgres' "
            "ORDER BY datname"
        )

    def database_exists(self, db: str) -> bool:
        return self.scalar(f"SELECT 1 FROM pg_database WHERE datname='{db}'") == "1"

    def role_exists(self, role: str) -> bool:
        return self.scalar(f"SELECT 1 FROM pg_roles WHERE rolname='{role}'") == "1"

    def database_owner(self, db: str) -> str | None:
        """À capturer AVANT un dropdb : le propriétaire disparaît avec la base."""
        out = self.scalar(
            "SELECT pg_get_userbyid(datdba) FROM pg_database "
            f"WHERE datname='{db}'"
        )
        return out or None

    def database_size_mb(self, db: str) -> int:
        return int(self.scalar(f"SELECT ceil(pg_database_size('{db}')/1024.0/1024)"))

    def database_acl(self, db: str) -> str:
        """Vide = privilèges par défaut, donc PUBLIC peut se connecter."""
        return self.scalar(
            "SELECT coalesce(array_to_string(datacl, ' '), '') "
            f"FROM pg_database WHERE datname='{db}'"
        )

    def hba_rules(self) -> list[list[str]]:
        """Ce qui est RÉELLEMENT chargé — un reload réussi ne le prouve pas."""
        return self.rows(
            "SELECT line_number, type, array_to_string(database,','), "
            "array_to_string(user_name,','), coalesce(address,''), auth_method, "
            "coalesce(error,'') FROM pg_hba_file_rules ORDER BY line_number"
        )

    def terminate_backends(self, db: str) -> int:
        out = self.runner.write(
            *self._argv(
                None,
                [
                    "-c",
                    "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                    f"WHERE datname='{db}' AND pid <> pg_backend_pid()",
                ],
            )
        ).out
        return int(out or 0)


# ─── Proxmox ─────────────────────────────────────────────────────────────────


class Pct:
    """Conteneurs LXC. Toujours exécuté sur le nœud, jamais dedans."""

    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    def exists(self, ctid: int) -> bool:
        return self.runner.probe("pct", "config", str(ctid))

    def status(self, ctid: int) -> str:
        return self.runner.read("pct", "status", str(ctid)).out.split()[-1]

    def running(self, ctid: int) -> bool:
        return self.status(ctid) == "running"

    def config(self, ctid: int) -> dict[str, str]:
        """`pct config` en dictionnaire. Format : `clé: valeur` par ligne."""
        out = self.runner.read("pct", "config", str(ctid))
        conf: dict[str, str] = {}
        for line in out.lines:
            key, _, value = line.partition(":")
            conf[key.strip()] = value.strip()
        return conf

    def set(self, ctid: int, **options: str) -> Result:
        argv = ["pct", "set", str(ctid)]
        for key, value in options.items():
            argv += [f"--{key}", str(value)]
        return self.runner.write(*argv)

    def start(self, ctid: int) -> Result:
        return self.runner.write("pct", "start", str(ctid))

    def reboot(self, ctid: int) -> Result:
        return self.runner.write("pct", "reboot", str(ctid))

    def push(self, ctid: int, src: Path, dst: str, *, perms: str = "0644") -> Result:
        return self.runner.write(
            "pct", "push", str(ctid), str(src), dst, "--perms", perms
        )

    @contextmanager
    def unprotected(self, ctid: int) -> Iterator[None]:
        """Lève la protection et la REMET, y compris sur exception.

        Remplace le trio variable globale + trap EXIT + astuce de portée bash.
        La protection interdit toute modification de disque, ajout de point de
        montage compris — et l'oublier au retour ne produit aucune erreur.
        """
        was_protected = self.config(ctid).get("protection") == "1"
        if not was_protected:
            yield
            return
        info(f"  levée temporaire de la protection du CT {ctid}")
        self.set(ctid, protection="0")
        try:
            yield
        finally:
            if self.config(ctid).get("protection") != "1":
                self.set(ctid, protection="1")
                info(f"  protection du CT {ctid} rétablie")


# ─── systemd ─────────────────────────────────────────────────────────────────


class Systemd:
    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    def is_active(self, unit: str) -> bool:
        return self.runner.probe("systemctl", "is-active", "--quiet", unit)

    def is_enabled(self, unit: str) -> bool:
        return self.runner.probe("systemctl", "is-enabled", "--quiet", unit)

    def exists(self, unit: str) -> bool:
        return bool(
            self.runner.read(
                "systemctl", "list-unit-files", unit, check=False
            ).out
        )

    def show(self, unit: str, prop: str) -> str:
        res = self.runner.read("systemctl", "show", unit, "-p", prop, "--value")
        return res.out

    def next_run(self, timer: str) -> str:
        return self.show(timer, "NextElapseUSecRealtime")

    def daemon_reload(self) -> Result:
        return self.runner.write("systemctl", "daemon-reload")

    def enable_now(self, unit: str) -> Result:
        return self.runner.write("systemctl", "enable", "--now", unit)

    def start(self, unit: str) -> Result:
        return self.runner.write("systemctl", "start", unit)

    def restart(self, unit: str) -> Result:
        return self.runner.write("systemctl", "restart", unit)

    def reload(self, unit: str) -> Result:
        return self.runner.write("systemctl", "reload", unit)

    def journal(self, unit: str, *, lines: int = 20) -> list[str]:
        return self.runner.read(
            "journalctl", "-u", unit, "-n", str(lines), "--no-pager", check=False
        ).lines


# ─── rclone ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RcloneConfig:
    remote: str = "gcs"
    bucket: str = ""
    config: Path = Path("/root/.config/rclone/rclone.conf")
    binary: str = "/usr/bin/rclone"  # absolu : PATH systemd minimal
    transfers: int = 4


class Rclone:
    """Copie hors-site.

    Le compte de service a objectViewer + objectCreator : il peut lister et
    écrire, pas écraser ni supprimer. D'où `--ignore-existing` sur copy et
    l'absence délibérée de toute méthode `delete` ou `sync` — un objet
    divergent est une anomalie à signaler, pas à corriger d'ici.
    """

    def __init__(self, runner: Runner, cfg: RcloneConfig) -> None:
        self.runner = runner
        self.cfg = cfg

    def _base(self) -> list[str]:
        return [self.cfg.binary, "--config", str(self.cfg.config)]

    def path(self, *parts: str) -> str:
        return f"{self.cfg.remote}:{self.cfg.bucket}/" + "/".join(parts)

    @property
    def version(self) -> str:
        return self.runner.read(*self._base(), "version").lines[0].split()[1]

    def list_files(self, remote: str) -> list[str]:
        """Chemins relatifs. Un préfixe inexistant renvoie une liste vide."""
        return self.runner.read(
            *self._base(), "lsf", "--files-only", "-R", remote
        ).lines

    def copy(self, local: Path, remote: str) -> Result:
        # copy, jamais sync : sync répliquerait les suppressions locales.
        return self.runner.write(
            *self._base(),
            "copy",
            str(local),
            remote,
            "--ignore-existing",
            "--transfers",
            str(self.cfg.transfers),
        )

    def check(self, local: Path, remote: str) -> tuple[bool, str]:
        """Vrai si le distant correspond. Porte sur TOUT l'instantané."""
        res = self.runner.read(
            *self._base(), "check", str(local), remote, "--one-way", check=False
        )
        return res.ok, res.stderr.strip()