"""Couche d'exécution — tout ce qui sort du processus passe par ici.

Trois idées portent ce module.

LECTURE / ÉCRITURE. Une commande qui observe et une commande qui modifie n'ont
pas le même statut en mode simulation : `check()` doit pouvoir interroger le
système même sous --dry-run, sinon il n'a rien à comparer. Le bash actuel
traite ça implicitement — les lectures sont écrites directement, les écritures
passent par `run` — ce qui marche tant que personne ne se trompe. Ici c'est
dans la signature : `read()` s'exécute toujours, `write()` est neutralisée.

EXÉCUTEUR INTERCHANGEABLE. `Psql`, `Systemd` et les autres ne savent pas où
ils tournent. Le même objet interroge le cluster depuis le conteneur (Local)
ou depuis le nœud (InContainer, qui préfixe par `pct exec`). C'est ce qui
évite d'écrire deux fois la même logique.

JAMAIS DE CHAÎNE SHELL. Tout est un argv passé tel quel à subprocess. Le
triple échappement Python → pct → shell du conteneur n'existe pas, parce
qu'aucun shell n'intervient.
"""

from __future__ import annotations

import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Protocol, Sequence

from .log import error, info, warn

DEFAULT_TIMEOUT = 300


class CommandError(RuntimeError):
    """Commande terminée sur un code non nul, avec de quoi diagnostiquer."""

    def __init__(self, result: "Result") -> None:
        self.result = result
        super().__init__(
            f"{' '.join(result.argv)} → code {result.code}\n{result.stderr.strip()}"
        )


@dataclass(frozen=True)
class Result:
    argv: tuple[str, ...]
    code: int
    stdout: str
    stderr: str
    skipped: bool = False  # neutralisée par --dry-run

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def out(self) -> str:
        return self.stdout.strip()

    @property
    def lines(self) -> list[str]:
        return [ln for ln in self.stdout.splitlines() if ln.strip()]


# ─── Exécuteurs ──────────────────────────────────────────────────────────────


class Executor(Protocol):
    """Transforme un argv logique en argv réellement lancé."""

    name: str

    def build(self, argv: Sequence[str]) -> list[str]: ...


@dataclass(frozen=True)
class Local:
    """Ici même — dans le conteneur, ou sur le nœud pour ses propres outils."""

    name: str = "local"

    def build(self, argv: Sequence[str]) -> list[str]:
        return list(argv)


@dataclass(frozen=True)
class InContainer:
    """Depuis le nœud, à destination d'un CT.

    `pct exec` transmet l'argv sans l'interpréter : aucun échappement à faire,
    et le code de retour de la commande distante devient celui de pct.
    """

    ctid: int
    name: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", f"ct:{self.ctid}")

    def build(self, argv: Sequence[str]) -> list[str]:
        return ["pct", "exec", str(self.ctid), "--", *argv]


# ─── Runner ──────────────────────────────────────────────────────────────────


class Runner:
    """Point de passage unique vers l'extérieur du processus.

    En mode simulation, seules les écritures sont neutralisées : un `check()`
    qui ne pourrait plus lire l'état ne servirait à rien.
    """

    def __init__(
        self,
        executor: Executor | None = None,
        *,
        dry_run: bool = False,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.executor: Executor = executor or Local()
        self.dry_run = dry_run
        self.timeout = timeout

    def for_container(self, ctid: int) -> "Runner":
        """Même configuration, mais à destination d'un conteneur."""
        return Runner(InContainer(ctid), dry_run=self.dry_run, timeout=self.timeout)

    # -- lecture : toujours exécutée --------------------------------------

    def read(
        self, *argv: str, check: bool = True, stdin: str | None = None
    ) -> Result:
        return self._run(argv, check=check, stdin=stdin)

    def probe(self, *argv: str) -> bool:
        """Vrai si la commande réussit. Pour les tests d'existence."""
        return self._run(argv, check=False).ok

    # -- écriture : neutralisée en simulation ------------------------------

    def write(
        self, *argv: str, check: bool = True, stdin: str | None = None
    ) -> Result:
        if self.dry_run:
            info(f"  [simulation] {' '.join(argv)}")
            return Result(tuple(argv), 0, "", "", skipped=True)
        return self._run(argv, check=check, stdin=stdin)

    # -- interne -----------------------------------------------------------

    def _run(
        self, argv: Sequence[str], *, check: bool, stdin: str | None = None
    ) -> Result:
        full = self.executor.build(argv)
        try:
            proc = subprocess.run(
                full,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise CommandError(
                Result(tuple(full), 127, "", f"exécutable introuvable : {exc.filename}")
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CommandError(
                Result(tuple(full), 124, "", f"délai dépassé ({exc.timeout}s)")
            ) from exc

        result = Result(tuple(full), proc.returncode, proc.stdout, proc.stderr)
        if check and not result.ok:
            raise CommandError(result)
        return result

    def which(self, binary: str) -> str | None:
        """Chemin absolu d'un exécutable, ou None.

        Toujours résoudre : systemd et `pct exec` fournissent un PATH minimal
        qui n'inclut pas /usr/local/bin.
        """
        if isinstance(self.executor, Local):
            return shutil.which(binary)
        res = self.read("command", "-v", binary, check=False)
        return res.out or None


# ─── Système de fichiers ─────────────────────────────────────────────────────


class Fs:
    """Opérations locales sur fichiers, soumises au même mode simulation.

    Volontairement séparé de Runner : pathlib fait mieux et plus lisiblement
    que d'appeler `ln`, `install` ou `mkdir` en sous-processus.
    """

    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    def symlink(self, target: Path, link: Path) -> bool:
        """Pose un lien symbolique. Renvoie True si quelque chose a changé."""
        if link.is_symlink() and link.readlink() == target:
            return False
        if self.dry_run:
            info(f"  [simulation] ln -sfn {target} {link}")
            return True
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(target)
        return True

    def install(self, src: Path, dst: Path, *, mode: int = 0o644) -> bool:
        """Copie si le contenu ou le mode diffèrent. Renvoie True si changé."""
        same = (
            dst.exists()
            and dst.read_bytes() == src.read_bytes()
            and (dst.stat().st_mode & 0o777) == mode
        )
        if same:
            return False
        if self.dry_run:
            info(f"  [simulation] install -m {mode:o} {src} {dst}")
            return True
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
        dst.chmod(mode)
        return True

    def mkdir(self, path: Path, *, mode: int = 0o755) -> bool:
        if path.is_dir():
            return False
        if self.dry_run:
            info(f"  [simulation] mkdir -p {path}")
            return True
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(mode)
        return True

    def remove(self, path: Path) -> bool:
        if not path.exists() and not path.is_symlink():
            return False
        if self.dry_run:
            info(f"  [simulation] rm {path}")
            return True
        path.unlink()
        return True


# ─── Aide au test ────────────────────────────────────────────────────────────


class FakeRunner(Runner):
    """Enregistre les appels sans rien exécuter.

    Permet de tester les décisions de convergence sans infrastructure : on
    alimente `responses` avec ce que le système est censé répondre, et on
    vérifie `calls` à la sortie.
    """

    def __init__(self, responses: dict[str, Result] | None = None) -> None:
        super().__init__(Local(), dry_run=False)
        self.responses = responses or {}
        self.calls: list[tuple[str, ...]] = []

    def _run(self, argv, *, check: bool, stdin: str | None = None) -> Result:
        self.calls.append(tuple(argv))
        key = " ".join(argv)
        if key in self.responses:
            result = self.responses[key]
            if check and not result.ok:
                raise CommandError(result)
            return result
        return Result(tuple(argv), 0, "", "")


@contextmanager
def guard(label: str) -> Iterator[None]:
    """Enrobe une opération : une erreur de commande devient un message situé."""
    try:
        yield
    except CommandError as exc:
        error(f"{label} : {exc.result.argv[0]} a échoué (code {exc.result.code})")
        if exc.result.stderr.strip():
            for line in exc.result.stderr.strip().splitlines():
                error(f"         {line}")
        raise