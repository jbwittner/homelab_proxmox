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

import os
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Protocol, Sequence

from .log import CONT, error, info

# Une commande ordinaire qui dépasse ça est bloquée, pas lente. Mais certaines
# ont légitimement le droit de durer : une unité qui accorde TimeoutStartSec=2h
# à un transfert, une sauvegarde de plusieurs gigaoctets. Ces appels-là passent
# `timeout=None` et laissent systemd être la seule horloge — sinon un transfert
# sain remonterait un code 124 qui n'est dans aucune table de retour.
DEFAULT_TIMEOUT = 300


class Secret(str):
    """Une valeur qui ne doit jamais atterrir dans un journal.

    Se comporte comme la chaîne qu'elle est — `subprocess` la passe telle
    quelle — mais `Runner` la remplace par `***` dans le `Result`, donc dans
    tout message d'erreur qui en découle.

    Sans elle, un `CREATE ROLE … PASSWORD '…'` qui échoue écrit le mot de passe
    dans `journalctl` : `CommandError` imprime l'argv complet. Le bash, lui,
    n'affiche le mot de passe qu'une fois, délibérément, et jamais dans une
    trace d'échec.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - confort de débogage
        return "Secret('***')"


def _mask(argv: Sequence[str]) -> tuple[str, ...]:
    return tuple("***" if isinstance(a, Secret) else a for a in argv)


# Les lignes qu'un outil écrit sur la sortie d'erreur SANS que ce soit la
# cause de quoi que ce soit. Elles arrivent en TÊTE, avant que la commande
# lancée n'ait écrit un mot — d'où le piège : lire la première ligne, c'est
# lire le bruit.
#
# `pct` est un programme Perl. Quand la locale de la session n'existe pas dans
# le conteneur, il émet ses propres avertissements sur le canal d'erreur de la
# commande distante. Constaté le 21 août 2026 :
#
#     KO  connexion à la base — perl: warning: Setting locale failed.
#
# Le vrai message, un `FATAL:` de PostgreSQL, était plus bas.
_BRUIT = (
    "perl: warning",
    "locale: cannot set",
    "setting locale failed",
    "falling back to the standard locale",
    "are supported and installed on your system",
    "language = ",
    "lc_all = ",
    "lang = ",
)

# Ce qui, à l'inverse, désigne la cause — en DEUX niveaux, et l'ordre compte.
#
# Un serveur qui refuse dit deux choses, dans cet ordre :
#
#     psql: error: connection to server at "192.168.1.56" failed
#     FATAL:  no pg_hba.conf entry for host "192.168.1.57"
#
# La première annonce l'échec, la seconde dit POURQUOI. Prendre la première
# rendrait « la connexion a échoué » — vrai, et parfaitement inutile.
_VERDICT = ("fatal:", "panic:", "does not exist", "denied", "refused")
_ECHEC = ("error:", "erreur", "could not", "cannot ", "failed")


def ligne_utile(stderr: str) -> str:
    """La ligne d'une sortie d'erreur qui DIT quelque chose.

    Quatre paris, dans cet ordre, et le dernier est le plus important :

      1. une ligne qui porte un VERDICT (`FATAL:`, « does not exist »…) —
         c'est celle qui dit pourquoi ;
      2. à défaut, une ligne qui annonce un ÉCHEC (`error:`, « failed »…) ;
      3. à défaut, la DERNIÈRE ligne non bruitée — le bruit vient en tête, le
         verdict en queue ;
      4. **s'il ne reste que du bruit, le dire.** Rendre le bruit serait
         affirmer une fausse piste, et une fausse piste plausible coûte plus
         cher qu'un aveu d'ignorance : on part corriger ce qui n'est pas cassé.
    """
    lignes = [ligne.strip() for ligne in stderr.splitlines() if ligne.strip()]
    utiles = [
        ligne for ligne in lignes
        if not any(motif in ligne.lower() for motif in _BRUIT)
    ]

    for motifs in (_VERDICT, _ECHEC):
        for ligne in utiles:
            if any(motif in ligne.lower() for motif in motifs):
                return ligne
    if utiles:
        return utiles[-1]
    if lignes:
        return "aucun message — seulement des avertissements de locale"
    return "aucun message"


class CommandError(RuntimeError):
    """Commande terminée sur un code non nul, avec de quoi diagnostiquer."""

    def __init__(self, result: "Result") -> None:
        self.result = result
        super().__init__(
            f"{' '.join(result.argv)} → code {result.code}\n{result.stderr.strip()}"
        )


@dataclass(frozen=True)
class Result:
    # argv est TOUJOURS la version masquée : un secret ne doit pas pouvoir
    # ressortir d'ici, ni par CommandError, ni par un repr() en débogage.
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
        timeout: int | None = DEFAULT_TIMEOUT,
    ) -> None:
        self.executor: Executor = executor or Local()
        self.dry_run = dry_run
        self.timeout = timeout

    def for_container(self, ctid: int) -> "Runner":
        """Même configuration, mais à destination d'un conteneur."""
        return Runner(InContainer(ctid), dry_run=self.dry_run, timeout=self.timeout)

    # -- lecture : toujours exécutée --------------------------------------

    def read(
        self,
        *argv: str,
        check: bool = True,
        stdin: str | None = None,
        timeout: int | None = -1,
        stream: bool = False,
    ) -> Result:
        return self._dispatch(
            argv, check=check, stdin=stdin, timeout=timeout, stream=stream
        )

    def probe(self, *argv: str) -> bool:
        """Vrai si la commande réussit. Pour les tests d'existence."""
        return self._dispatch(argv, check=False).ok

    # -- écriture : neutralisée en simulation ------------------------------

    def write(
        self,
        *argv: str,
        check: bool = True,
        stdin: str | None = None,
        timeout: int | None = -1,
        stream: bool = False,
    ) -> Result:
        if self.dry_run:
            info(f"  [dry-run] {' '.join(_mask(argv))}")
            return Result(_mask(argv), 0, "", "", skipped=True)
        return self._dispatch(
            argv, check=check, stdin=stdin, timeout=timeout, stream=stream
        )

    def exec_replace(self, *argv: str) -> None:
        """Remplace ce processus par la commande. Ne rend jamais la main.

        Pour déléguer une commande INTERACTIVE — le terminal, l'entrée standard
        et le code de retour passent sans intermédiaire. Une capture par tuyau
        les perdrait, et une question posée à l'autre bout resterait muette.

        Sous --dry-run on annonce et on revient : il n'y a rien à remplacer.
        """
        full = self.executor.build(argv)
        if self.dry_run:
            info(f"  [dry-run] {' '.join(_mask(full))}")
            return
        os.execvp(full[0], full)

    # -- interne -----------------------------------------------------------

    def _dispatch(
        self,
        argv: Sequence[str],
        *,
        check: bool,
        stdin: str | None = None,
        timeout: int | None = -1,
        stream: bool = False,
    ) -> Result:
        # -1 est le marqueur « rien de précisé » : None est une valeur utile,
        # elle veut dire « aucune limite », et doit pouvoir être demandée.
        effective = self.timeout if timeout == -1 else timeout
        if stream:
            return self._run_streamed(argv, check=check, timeout=effective)
        return self._run(argv, check=check, stdin=stdin, timeout=effective)

    def _run(
        self,
        argv: Sequence[str],
        *,
        check: bool,
        stdin: str | None = None,
        timeout: int | None = DEFAULT_TIMEOUT,
    ) -> Result:
        full = self.executor.build(argv)
        safe = _mask(full)
        try:
            proc = subprocess.run(
                full,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise CommandError(
                Result(safe, 127, "", f"exécutable introuvable : {exc.filename}")
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CommandError(
                Result(safe, 124, "", f"délai dépassé ({exc.timeout}s)")
            ) from exc

        result = Result(safe, proc.returncode, proc.stdout, proc.stderr)
        if check and not result.ok:
            raise CommandError(result)
        return result

    def _run_streamed(
        self,
        argv: Sequence[str],
        *,
        check: bool,
        timeout: int | None = None,
    ) -> Result:
        """Recopie la sortie au fil de l'eau, indentée, et la conserve.

        `capture_output` ne rend la main qu'à la fin : un `rclone copy` de
        quarante minutes n'écrirait rien dans le journal avant de se terminer,
        et le préfixe d'alignement du bash serait perdu. On lit donc ligne à
        ligne.

        Les deux flux sont fusionnés, comme le fait `2>&1 | indent` en bash :
        pour une commande longue, l'entrelacement chronologique vaut mieux que
        la séparation.

        La limite de temps ne peut s'appliquer qu'à l'attente finale — c'est
        assumé : une commande qu'on diffuse est une commande qu'on laisse
        durer, et systemd reste l'horloge qui compte.
        """
        full = self.executor.build(argv)
        safe = _mask(full)
        collected: list[str] = []
        try:
            proc = subprocess.Popen(
                full,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise CommandError(
                Result(safe, 127, "", f"exécutable introuvable : {exc.filename}")
            ) from exc

        assert proc.stdout is not None
        with proc.stdout:
            for line in proc.stdout:
                line = line.rstrip("\n")
                collected.append(line)
                print(f"{CONT}{line}", flush=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait()
            raise CommandError(
                Result(safe, 124, "\n".join(collected), f"délai dépassé ({exc.timeout}s)")
            ) from exc

        result = Result(safe, code, "\n".join(collected), "")
        if check and not result.ok:
            raise CommandError(result)
        return result

    def which(self, binary: str) -> str | None:
        """Chemin absolu d'un exécutable, ou None.

        Toujours résoudre : systemd et `pct exec` fournissent un PATH minimal
        qui n'inclut pas /usr/local/bin.

        Côté conteneur il faut un shell, `command` étant une primitive et non
        un binaire : `pct exec` fait un execvp et ne trouverait rien. Le script
        shell est une CONSTANTE et le nom cherché arrive en argument — rien
        n'est concaténé, donc rien n'est interprétable.
        """
        if isinstance(self.executor, Local):
            return shutil.which(binary)
        res = self.read(
            "sh", "-c", 'command -v "$1" || true', "sh", binary, check=False
        )
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
            info(f"  [dry-run] ln -sfn {target} {link}")
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
            info(f"  [dry-run] install -m {mode:o} {src} {dst}")
            return True
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
        dst.chmod(mode)
        return True

    def write_file(self, path: Path, content: str, *, mode: int = 0o644) -> bool:
        """Écrit un contenu FABRIQUÉ. Renvoie True si quelque chose a changé.

        `install()` recopie un fichier du dépôt ; certains fichiers, eux,
        n'existent nulle part avant d'être calculés — un drop-in qui porte le
        nom du nœud, une configuration qui porte un chemin de clé. Ils passent
        par ici pour hériter des deux mêmes garanties : la simulation n'écrit
        rien, et un contenu déjà identique n'est pas réécrit — sans quoi le
        mtime changerait et le bilan annoncerait une pose qui n'a pas eu lieu.
        """
        same = (
            path.is_file()
            and path.read_text() == content
            and (path.stat().st_mode & 0o777) == mode
        )
        if same:
            return False
        if self.dry_run:
            info(f"  [dry-run] écrire {path} ({mode:o})")
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)
        return True

    def mkdir(self, path: Path, *, mode: int = 0o755) -> bool:
        if path.is_dir():
            return False
        if self.dry_run:
            info(f"  [dry-run] mkdir -p {path}")
            return True
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(mode)
        return True

    def remove(self, path: Path) -> bool:
        if not path.exists() and not path.is_symlink():
            return False
        if self.dry_run:
            info(f"  [dry-run] rm {path}")
            return True
        path.unlink()
        return True


# ─── Aide au test ────────────────────────────────────────────────────────────


class FakeRunner(Runner):
    """Enregistre les appels sans rien exécuter.

    Permet de tester les décisions de convergence sans infrastructure : on
    alimente `responses` avec ce que le système est censé répondre, et on
    vérifie `calls` à la sortie.

    Deux formes de correspondance, parce qu'une seule ne suffit pas :

      - la chaîne exacte, quand l'argv est court et stable ;
      - un prédicat, quand il ne l'est pas. Une ligne de commande `rclone`
        porte une demi-douzaine de drapeaux ; l'exiger au caractère près
        transformerait chaque ajout d'option en cascade de tests rouges, pour
        une raison qui n'aurait rien à voir avec ce qu'ils vérifient.

    Les prédicats sont essayés dans l'ordre, après la table exacte.
    """

    def __init__(
        self,
        responses: dict[str, Result] | None = None,
        matchers: list[tuple[object, Result]] | None = None,
    ) -> None:
        super().__init__(Local(), dry_run=False)
        self.responses = responses or {}
        self.matchers = matchers or []
        self.calls: list[tuple[str, ...]] = []
        # Ce qui a été poussé sur l'entrée standard, appel par appel. Certaines
        # commandes se pilotent par là et non par leur argv — psql substitue
        # ses variables sur l'entrée standard, jamais avec -c.
        self.stdins: list[str | None] = []

    def for_container(self, ctid: int) -> "FakeRunner":
        """Un double, pas un vrai Runner.

        `Runner.for_container` fabrique un Runner neuf ; hérité tel quel, il
        ferait sortir les tests du bac à sable et exécuterait `pct` pour de
        bon. L'enfant partage la table de réponses ET le journal des appels :
        ce qui part vers le conteneur se lit au même endroit que le reste.
        """
        enfant = FakeRunner(self.responses, self.matchers)
        enfant.executor = InContainer(ctid)
        enfant.dry_run = self.dry_run
        enfant.calls = self.calls
        enfant.stdins = self.stdins
        return enfant

    def when(self, predicate, result: Result) -> "FakeRunner":
        """Ajoute un prédicat. `predicate` reçoit l'argv en tuple.

        Une chaîne est acceptée comme raccourci : elle vaut « l'argv joint
        contient ce fragment ».
        """
        self.matchers.append((predicate, result))
        return self

    def _lookup(self, argv: tuple[str, ...]) -> Result | None:
        key = " ".join(argv)
        if key in self.responses:
            return self.responses[key]
        for predicate, result in self.matchers:
            if isinstance(predicate, str):
                if predicate in key:
                    return result
            elif predicate(argv):
                return result
        return None

    # Un seul point d'entrée à intercepter : `read`, `write` et `probe`
    # passent tous par `_dispatch`, y compris pour le mode diffusé.
    def _dispatch(
        self,
        argv,
        *,
        check: bool,
        stdin: str | None = None,
        timeout: int | None = -1,
        stream: bool = False,
    ) -> Result:
        # L'argv RÉELLEMENT lancé, préfixe `pct exec` compris : c'est ce qu'on
        # veut relire. Pour un exécuteur local, `build` ne change rien.
        argv = tuple(self.executor.build(argv))
        self.calls.append(argv)
        self.stdins.append(stdin)
        found = self._lookup(_mask(argv))
        result = found if found is not None else Result(_mask(argv), 0, "", "")
        if check and not result.ok:
            raise CommandError(result)
        return result


@contextmanager
def guard(label: str) -> Iterator[None]:
    """Enrobe une opération : une erreur de commande devient un message situé."""
    try:
        yield
    except CommandError as exc:
        error(f"{label} : {exc.result.argv[0]} a échoué (code {exc.result.code})")
        if exc.result.stderr.strip():
            for line in exc.result.stderr.strip().splitlines():
                error(f"{CONT}{line}")
        raise
