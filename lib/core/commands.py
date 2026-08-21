"""Wrappers d'outils — une classe par binaire, des méthodes plutôt que des argv.

Le reste du code n'écrit jamais `subprocess` ni `psql` : il appelle des
méthodes qui renvoient des types Python. Chaque particularité d'un outil — les
drapeaux `-tA` de psql, la citation des identifiants SQL, les options que
`rclone` exige sur ce bucket — est traitée ici, une fois, et testée une fois.

Aucune de ces classes ne sait où elle tourne : c'est le Runner qu'on leur passe
qui décide. `Psql(runner)` interroge le cluster local ; `Psql(runner.
for_container(200))` interroge celui du CT 200 depuis le nœud. Même code.

Rien ici ne connaît Proxmox. `pct` est l'affaire du nœud et vit dans
`proxmox.Container` : ce paquet-ci est poussé DANS les conteneurs, où `pct`
n'existe pas et n'aurait aucun sens.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .runner import Result, Runner, Secret

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

    def run_sql(self, sql: str, *, db: str | None = None, **params: str) -> Result:
        """Joue du SQL avec des variables psql, sans passer par un fichier.

        Le SQL part sur l'ENTRÉE STANDARD, jamais avec `-c`. psql ne substitue
        `:"var"` que lorsqu'il lit depuis un fichier ou depuis son entrée
        standard ; avec `-c`, la chaîne est transmise telle quelle au serveur,
        qui répond « syntax error at or near ":" ». C'est ce que faisait le
        heredoc du bash, et il avait raison.

        Même garantie que `run_file` : les identifiants arrivent par `-v` et
        c'est psql qui les cite. Rien n'est interpolé dans le texte SQL, donc
        un nom de rôle exotique ne peut pas en changer le sens.
        """
        extra: list[str] = []
        for key, value in params.items():
            pair = f"{key}={value}"
            extra += ["-v", Secret(pair) if isinstance(value, Secret) else pair]
        extra += ["-q"]
        return self.runner.write(*self._argv(db, extra), stdin=sql)

    def run_file(self, path: str, *, db: str | None = None, **params: str) -> Result:
        """Joue un script SQL avec des variables psql.

        Correspond à `psql -v name=<locataire> -v password=... -f <script>`.

        Une valeur de type `Secret` reste secrète jusque dans l'argv : le
        couple `clé=valeur` en hérite, donc un échec ne recopie pas le mot de
        passe dans le journal. C'est le seul chemin par lequel un secret doit
        atteindre psql.
        """
        extra: list[str] = []
        for key, value in params.items():
            pair = f"{key}={value}"
            extra += ["-v", Secret(pair) if isinstance(value, Secret) else pair]
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

    # -- outils de la famille (pg_dump, pg_restore, createdb, dropdb) --------
    #
    # Ce ne sont pas des commandes psql, mais ils partagent son utilisateur et
    # sa socket : les regrouper ici évite de réécrire le préfixe `sudo -u` à
    # chaque appel, et surtout de l'oublier une fois.

    def _sudo(self, *argv: str) -> list[str]:
        return ["sudo", "-u", self.user, *argv]

    def dump(self, db: str, fichier: Path) -> Result:
        """Sauvegarde d'une base au format personnalisé.

        `-f` plutôt qu'une redirection : aucun shell n'intervient, donc aucun
        échappement à faire sur le chemin. `--no-owner --no-acl` parce que ces
        deux-là se reposent à la restauration, à partir du propriétaire capturé
        avant destruction.
        """
        return self.runner.write(
            *self._sudo("pg_dump", "-Fc", "--no-owner", "--no-acl",
                        "-f", str(fichier), db),
            timeout=None,
        )

    def restore_dump(self, db: str, fichier: Path, *, role: str) -> Result:
        """`--role` est ce qui rend les tables au locataire. Sans lui elles
        appartiennent à postgres et le locataire ne peut plus rien en faire."""
        return self.runner.write(
            *self._sudo("pg_restore", "-d", db, "--no-owner", f"--role={role}",
                        str(fichier)),
            timeout=None,
            stream=True,
        )

    def createdb(self, db: str, *, owner: str) -> Result:
        """LC_COLLATE C : l'ordre ne dépend plus de la libc de la machine, et un
        index reste valide d'un hôte à l'autre."""
        return self.runner.write(
            *self._sudo("createdb", db, "-O", owner, "-T", "template0",
                        "--encoding", "UTF8", "--lc-collate", "C",
                        "--lc-ctype", "C")
        )

    def dropdb(self, db: str) -> Result:
        return self.runner.write(*self._sudo("dropdb", db))

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

    def environment(self, unit: str) -> dict[str, str]:
        """L'environnement que systemd donnera au processus.

        Drop-in compris, et c'est tout l'intérêt : c'est le drop-in qui porte
        les valeurs propres à CETTE machine. Une commande lancée à la main
        n'hérite de rien, donc lire le shell courant ne dirait rien de ce qui
        tournera cette nuit.

        Un fragment sans « = » est ignoré plutôt que de fabriquer une clé vide,
        qui écraserait ensuite une vraie valeur.
        """
        valeurs: dict[str, str] = {}
        for fragment in self.show(unit, "Environment").split():
            cle, sep, valeur = fragment.partition("=")
            if sep and cle:
                valeurs[cle] = valeur
        return valeurs

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
    retries: int = 3
    low_level_retries: int = 3
    bwlimit: str = ""  # vide = aucune limitation
    # « hash » compare les empreintes, « size » se contente de la taille.
    check_mode: str = "hash"


class Rclone:
    """Copie hors-site.

    Le compte de service a objectViewer + objectCreator : il peut lister et
    écrire, pas écraser ni supprimer. D'où `--ignore-existing` sur copy et
    l'absence délibérée de toute méthode `delete` ou `sync` — un objet
    divergent est une anomalie à signaler, pas à corriger d'ici. Ne pas les
    ajouter : cette absence EST la garantie.
    """

    def __init__(self, runner: Runner, cfg: RcloneConfig) -> None:
        self.runner = runner
        self.cfg = cfg

    def _base(self) -> list[str]:
        argv = [
            self.cfg.binary,
            "--config",
            str(self.cfg.config),
            "--retries",
            str(self.cfg.retries),
            "--low-level-retries",
            str(self.cfg.low_level_retries),
            "--stats",
            "0",
            # L'accès uniforme (UBLA) est activé sur le bucket : sans ce
            # drapeau, rclone joint une ACL héritée à chaque objet et le
            # transfert échoue en « Error 400: Cannot insert legacy ACL for an
            # object when uniform bucket-level access is enabled », zéro octet
            # écrit. Constaté le 20 août 2026, à la première exécution réelle.
            # Il double le « bucket_policy_only » de rclone.conf, à dessein :
            # le script doit marcher sur une configuration reconstruite à la
            # va-vite, et les deux ne se gênent pas.
            "--gcs-bucket-policy-only",
        ]
        if self.cfg.bwlimit:
            argv += ["--bwlimit", self.cfg.bwlimit]
        return argv

    def path(self, *parts: str) -> str:
        return f"{self.cfg.remote}:{self.cfg.bucket}/" + "/".join(parts)

    @property
    def version(self) -> str:
        return self.runner.read(*self._base(), "version").lines[0].split()[1]

    def reachable(self) -> tuple[bool, str]:
        """Le bucket répond-il ? Message d'erreur brut si non.

        Volontairement un `lsf` et non un `rclone about` : le compte de service
        est `objectViewer`, il n'a pas `storage.buckets.get` et `about`
        échouerait sur un bucket parfaitement sain.
        """
        res = self.runner.read(
            *self._base(),
            "lsf",
            "--max-depth",
            "1",
            f"{self.cfg.remote}:{self.cfg.bucket}",
            check=False,
        )
        return res.ok, (res.stdout + res.stderr).strip()

    def list_files(self, remote: str) -> list[str]:
        """Chemins relatifs. Un préfixe inexistant renvoie une liste vide.

        Lève `CommandError` si le listage lui-même échoue — à traduire par
        l'appelant en « transfert échoué », pas en « rien à copier » : la
        prochaine exécution réessaiera d'elle-même.
        """
        return self.runner.read(
            *self._base(), "lsf", "--files-only", "-R", remote
        ).lines

    def copy(self, local: Path, remote: str) -> Result:
        """copy, JAMAIS sync : sync répliquerait les suppressions locales.

        Diffusé et sans limite de temps : un transfert de plusieurs dizaines de
        minutes doit rester visible dans le journal pendant qu'il tourne, et
        c'est `TimeoutStartSec` de l'unité qui l'encadre.
        """
        return self.runner.write(
            *self._base(),
            "copy",
            str(local),
            remote,
            "--ignore-existing",
            "--transfers",
            str(self.cfg.transfers),
            stream=True,
            timeout=None,
        )

    def check(self, local: Path, remote: str) -> tuple[bool, str]:
        """Vrai si le distant correspond. Porte sur TOUT l'instantané.

        `--one-way` : ce qui existe en trop à distance ne nous regarde pas.
        C'est ici, et nulle part ailleurs, qu'un objet partiel laissé par une
        exécution interrompue se révèle.
        """
        argv = [*self._base(), "check", str(local), remote, "--one-way"]
        if self.cfg.check_mode == "size":
            argv.append("--size-only")
        res = self.runner.read(*argv, check=False, timeout=None)
        # rclone écrit son verdict sur stderr : les deux flux comptent.
        return res.ok, (res.stdout + res.stderr).strip()

    def size(self, remote: str) -> str:
        """Ligne de bilan, pour le journal. Jamais bloquant."""
        res = self.runner.read(*self._base(), "size", remote, check=False)
        return " ".join(res.lines)
