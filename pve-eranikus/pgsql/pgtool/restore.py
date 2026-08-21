"""Restauration d'une base — le chemin de secours.

TROIS CHOSES DOIVENT ARRIVER DANS CET ORDRE, et le reste en découle.

1. **Capturer le propriétaire AVANT de détruire la base.** Il disparaît avec
   elle. Capturé après, `pg_restore --role` n'aurait plus rien à quoi se
   raccrocher et les tables reviendraient à `postgres`, hors d'atteinte du
   locataire.
2. **Poser un filet avant d'écraser.** Un `pg_dump` de l'état actuel, dans
   `pre-restore-<horodatage>/`. Restaurer la mauvaise sauvegarde arrive ; ne
   plus pouvoir revenir en arrière ne doit pas.
3. **Réappliquer les ACL après le `pg_restore`.** Elles ne sont NI dans le dump
   NI dans `globals.sql` : sans cette étape, `PUBLIC` retrouve `CONNECT` et
   l'isolation entre locataires disparaît sans le moindre message. C'est
   pourquoi aucun drapeau ne permet de la sauter — il n'existe pas de cas
   légitime où l'on restaure une base de locataire sans son isolation.

CE QUI EST CORRIGÉ PAR RAPPORT AU BASH. La dernière instruction de
`cmd_restore` était `[[ -n ${pre:-} ]] && log …`, un test qui échoue quand la
base n'existait pas et qu'aucun filet n'a donc été posé. La fonction rendait 1
sur une restauration parfaitement réussie. Sans conséquence sur les données,
mais un appelant qui vérifie le code conclut à un échec.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.commands import Psql
from core.log import CONT, detail, info, step, warn
from core.runner import Runner
from pgtool.snapshots import Snapshot, Store


class RestoreError(RuntimeError):
    """Refus argumenté. Rien n'a été détruit."""


@dataclass(frozen=True)
class Report:
    database: str
    owner: str
    snapshot: str
    ok: bool
    safety_net: Path | None  # None = la base n'existait pas


@dataclass(frozen=True)
class VerifyReport:
    database: str
    tables: int
    acl: str
    public_can_connect: bool
    foreign_tables: int

    @property
    def isolated(self) -> bool:
        return not self.public_can_connect


def _stamp(maintenant: datetime | None = None) -> str:
    return (maintenant or datetime.now()).strftime("%Y%m%d-%H%M%S")


def restore(
    psql: Psql,
    runner: Runner,
    store: Store,
    *,
    database: str,
    ref: str = "latest",
    pre_dir: Path | None = None,
    maintenant: datetime | None = None,
) -> Report:
    """Restaure `database` depuis l'instantané `ref`. Écrase la base visée.

    La confirmation n'est PAS demandée ici : elle se pose côté nœud, là où il y
    a un terminal — `pct exec` n'alloue pas de TTY, et une question posée
    depuis le conteneur ne verrait jamais la réponse.
    """
    instantane: Snapshot = store.resolve(ref)
    dump = instantane.dump(database)
    if not dump.is_file():
        raise RestoreError(
            f"{database}.dump absent de {instantane.name} — voir « pg show »"
        )

    step(f"restauration de « {database} » depuis {instantane.name}")
    manifeste = instantane.manifest()
    if manifeste:
        detail("\n".join(f"{cle:<12}: {valeur}"
                         for cle, valeur in manifeste.items()))
    else:
        warn(f"  {instantane.name} : pas de MANIFEST")

    # 1. Le propriétaire, AVANT toute destruction.
    proprietaire = psql.database_owner(database) or database
    info(f"  propriétaire cible : {proprietaire}")
    if not psql.role_exists(proprietaire):
        raise RestoreError(
            f"le rôle {proprietaire} n'existe pas — le recréer avant\n"
            f"{CONT}les rôles se reposent d'abord : "
            f"psql -f {instantane.path}/globals.sql"
        )

    # 2. Le filet, si et seulement s'il y a quelque chose à sauver.
    filet: Path | None = None
    if psql.database_exists(database):
        racine = pre_dir or store.dest
        filet = racine / f"pre-restore-{_stamp(maintenant)}"
        filet.mkdir(parents=True, exist_ok=True)
        filet.chmod(0o700)
        step("filet de sécurité avant écrasement")
        psql.dump(database, filet / f"{database}.dump")
        info(f"  {filet / f'{database}.dump'}")

        fermees = psql.terminate_backends(database)
        info(f"  {fermees} session(s) fermée(s)")
        psql.dropdb(database)
    else:
        warn(f"  la base {database} n'existe pas — création")

    psql.createdb(database, owner=proprietaire)
    step("chargement du dump")
    psql.restore_dump(database, dump, role=proprietaire)

    # 3. Les ACL. Obligatoire, pas optionnel.
    step("réapplication des ACL")
    info("  ni le dump ni globals.sql ne les portent")
    _reapply_acl(psql, database=database, owner=proprietaire)

    step("restauration terminée")
    if filet is not None:
        info(f"  état précédent conservé dans {filet}")
    return Report(
        database=database,
        owner=proprietaire,
        snapshot=instantane.name,
        ok=True,
        safety_net=filet,
    )


def _reapply_acl(psql: Psql, *, database: str, owner: str) -> None:
    """Les deux moitiés de l'isolation d'un locataire.

    Au niveau de la base : qui peut s'y connecter. Au niveau du schéma : qui
    peut y créer. Les identifiants passent par des variables psql, qui citent
    elles-mêmes — rien n'est interpolé dans le SQL.
    """
    # `db=` choisit la connexion, les autres mots-clés sont des variables psql.
    # Deux rôles distincts, donc deux noms distincts : les confondre ferait
    # jouer le second bloc sur la mauvaise base.
    psql.run_sql(
        'REVOKE CONNECT ON DATABASE :"cible" FROM PUBLIC;\n'
        'GRANT  CONNECT ON DATABASE :"cible" TO :"proprietaire";\n',
        cible=database,
        proprietaire=owner,
    )
    psql.run_sql(
        "REVOKE ALL ON SCHEMA public FROM PUBLIC;\n"
        'ALTER  SCHEMA public OWNER TO :"proprietaire";\n'
        'GRANT  ALL ON SCHEMA public TO :"proprietaire";\n',
        db=database,
        proprietaire=owner,
    )


def verify(psql: Psql, *, database: str, owner: str | None = None) -> VerifyReport:
    """Contrôle après restauration. Rapporte, ne juge pas — jamais d'échec.

    Un avertissement ici n'est pas une panne : c'est une invitation à regarder.
    Faire échouer la commande masquerait le reste du rapport.
    """
    cible = owner or database
    step(f"contrôle de « {database} »")

    acl = psql.database_acl(database)
    # Les privilèges par défaut d'une base neuve autorisent PUBLIC à se
    # connecter, et un datacl vide veut dire « défaut ». C'est donc l'absence
    # d'ACL qui est le signal, autant que la présence d'un droit à PUBLIC.
    public = _public_can_connect(acl)
    if public:
        warn("  ACL : PUBLIC peut se connecter — isolation absente")
        warn(f'{CONT}REVOKE CONNECT ON DATABASE "{database}" FROM PUBLIC;')
    else:
        info(f"  ACL : {acl}")

    tables = int(psql.scalar(
        "SELECT count(*) FROM pg_tables WHERE schemaname='public'", db=database
    ) or 0)
    info(f"  tables (schéma public) : {tables}")

    # Comparé au propriétaire RÉEL, pas au nom de la base. Le bash comparait
    # `tableowner <> '<nom de la base>'` : une base dont le rôle porte un autre
    # nom déclenchait un avertissement à chaque contrôle, même après une
    # restauration parfaite.
    etrangeres = int(psql.scalar(
        "SELECT count(*) FROM pg_tables "
        f"WHERE schemaname='public' AND tableowner <> '{cible}'",
        db=database,
    ) or 0)
    if etrangeres:
        warn(f"  {etrangeres} table(s) n'appartiennent pas à {cible} "
             "— pg_restore sans --role ?")
    else:
        info("  propriétaire des tables : OK")

    return VerifyReport(
        database=database,
        tables=tables,
        acl=acl,
        public_can_connect=public,
        foreign_tables=etrangeres,
    )


def _public_can_connect(acl: str) -> bool:
    """Un `datacl` vide vaut « privilèges par défaut », donc PUBLIC connecté.

    Sinon on cherche une entrée dont le bénéficiaire est vide — la façon dont
    PostgreSQL écrit PUBLIC : `=Tc/postgres`. Le bash cherchait la sous-chaîne
    « =Tc/ » n'importe où, ce qui matchait aussi `forge=Tc/postgres`, un droit
    accordé au locataire lui-même.
    """
    if not acl.strip():
        return True
    for entree in acl.strip().strip("{}").split(","):
        beneficiaire, _, droits = entree.strip().partition("=")
        if beneficiaire == "" and "c" in droits.split("/")[0].lower():
            return True
    return False
