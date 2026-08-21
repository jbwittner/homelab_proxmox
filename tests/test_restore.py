"""Restauration : l'ordre des opérations, et ce qu'aucun dump ne contient.

C'est un chemin de secours. Les tests portent donc moins sur le résultat que
sur l'ORDRE — capturer le propriétaire avant de détruire la base — et sur ce
qui doit arriver même quand personne ne le demande : les ACL.
"""

from __future__ import annotations

import pytest

from core.commands import Psql
from core.runner import FakeRunner, Result
from pgtool.restore import RestoreError, restore, verify
from pgtool.snapshots import Store


# ─── mise en place ───────────────────────────────────────────────────────────


@pytest.fixture
def instantane(tmp_path):
    dest = tmp_path / "postgresql"
    d = dest / "20260820-093240"
    d.mkdir(parents=True)
    for f in ("globals.sql", "forge.dump", "MANIFEST"):
        (d / f).write_text(f"contenu de {f}\n")
    return Store(dest)


def _cluster(*, base_existe: bool, role_existe: bool = True,
             proprietaire: str = "forge") -> FakeRunner:
    """Un cluster qui répond comme psql, sans psql."""
    r = FakeRunner()

    def repond(fragment: str, valeur: str):
        r.when(lambda argv, f=fragment: any(f in a for a in argv),
               Result(("psql",), 0, valeur + "\n", ""))

    repond("pg_get_userbyid", proprietaire if base_existe else "")
    repond("FROM pg_roles", "1" if role_existe else "")
    repond("FROM pg_database WHERE datname", "1" if base_existe else "")
    repond("pg_terminate_backend", "3")
    repond("FROM pg_tables", "12")
    return r


def _sql_envoye(runner: FakeRunner) -> str:
    return "\n".join(" ".join(argv) for argv in runner.calls)


def _indice(runner: FakeRunner, binaire: str) -> int:
    """Rang du premier appel à `binaire`, par ÉGALITÉ d'élément.

    Surtout pas une sous-chaîne : pytest nomme le répertoire temporaire d'après
    le test, si bien qu'un test appelé « …pg_restore… » voit sa chaîne
    apparaître dans le chemin passé à pg_dump. La correspondance approximative
    désignait alors le mauvais appel, et l'assertion parlait d'autre chose que
    de ce qu'elle croyait.
    """
    for i, argv in enumerate(runner.calls):
        if binaire in argv:
            return i
    return -1


def _indice_sql(runner: FakeRunner, fragment: str) -> int:
    """Rang du premier ordre SQL contenant `fragment`.

    Cherché dans le seul argument qui suit `-c`, pour ne pas retomber sur un
    chemin de fichier qui contiendrait les mêmes mots.
    """
    for i, argv in enumerate(runner.calls):
        if "-c" in argv:
            sql = argv[argv.index("-c") + 1]
            if fragment in sql:
                return i
    return -1


# ─── LE DÉFAUT DU BASH ───────────────────────────────────────────────────────


def test_restaurer_une_base_absente_reussit_avec_le_code_zero(instantane, tmp_path):
    """Reproduit le défaut de `cmd_restore` : sa dernière instruction est
    `[[ -n ${pre:-} ]] && log …`, un test qui ÉCHOUE quand la base n'existait
    pas et qu'aucun filet n'a été posé. La fonction rendait donc 1 alors que la
    restauration avait réussi.

        cmd_restore_base_absente() { local pre; [[ -n ${pre:-} ]] && echo x; }
        cmd_restore_base_absente; echo $?   →  1

    Sans conséquence sur les données, mais un appelant qui vérifie le code
    conclut à un échec.
    """
    r = _cluster(base_existe=False)
    rapport = restore(
        Psql(r), r, instantane, database="forge", ref="20260820-093240",
        pre_dir=tmp_path / "filets",
    )
    assert rapport.ok is True
    assert rapport.safety_net is None, "aucun filet : il n'y avait rien à sauver"


def test_restaurer_une_base_existante_reussit_aussi(instantane, tmp_path):
    r = _cluster(base_existe=True)
    rapport = restore(
        Psql(r), r, instantane, database="forge", ref="20260820-093240",
        pre_dir=tmp_path / "filets",
    )
    assert rapport.ok is True
    assert rapport.safety_net is not None


# ─── l'ordre, qui n'est pas négociable ───────────────────────────────────────


def test_le_proprietaire_est_capture_AVANT_le_dropdb(instantane, tmp_path):
    """Il disparaît avec la base. Capturé après, `pg_restore --role` n'aurait
    plus rien à quoi se raccrocher, et les tables reviendraient à postgres."""
    r = _cluster(base_existe=True)
    restore(Psql(r), r, instantane, database="forge", ref="20260820-093240",
            pre_dir=tmp_path / "filets")
    capture = _indice_sql(r, "pg_get_userbyid")
    drop = _indice(r, "dropdb")
    assert capture != -1 and drop != -1
    assert capture < drop, "capturer le propriétaire après le dropdb est trop tard"


def test_le_filet_est_pris_avant_le_dropdb(instantane, tmp_path):
    r = _cluster(base_existe=True)
    restore(Psql(r), r, instantane, database="forge", ref="20260820-093240",
            pre_dir=tmp_path / "filets")
    assert _indice(r, "pg_dump") < _indice(r, "dropdb")


def test_les_sessions_sont_fermees_avant_le_dropdb(instantane, tmp_path):
    """Sinon `dropdb` échoue sur « database is being accessed by other users »."""
    r = _cluster(base_existe=True)
    restore(Psql(r), r, instantane, database="forge", ref="20260820-093240",
            pre_dir=tmp_path / "filets")
    assert _indice_sql(r, "pg_terminate_backend") < _indice(r, "dropdb")


def test_les_acl_sont_reappliquees_apres_le_pg_restore(instantane, tmp_path):
    r = _cluster(base_existe=True)
    restore(Psql(r), r, instantane, database="forge", ref="20260820-093240",
            pre_dir=tmp_path / "filets")
    assert _indice(r, "pg_restore") < _indice_sql(r, "REVOKE CONNECT")


# ─── ce qu'aucun dump ne contient ────────────────────────────────────────────


def test_les_acl_sont_reappliquees_meme_sur_une_base_neuve(instantane, tmp_path):
    """Elles ne sont NI dans le dump NI dans globals.sql. Sans cette étape,
    PUBLIC retrouve CONNECT et l'isolation entre locataires disparaît — en
    silence, ce qui est le pire des cas."""
    r = _cluster(base_existe=False)
    restore(Psql(r), r, instantane, database="forge", ref="20260820-093240",
            pre_dir=tmp_path / "filets")
    sql = _sql_envoye(r)
    assert "REVOKE CONNECT ON DATABASE" in sql
    assert "REVOKE ALL ON SCHEMA public FROM PUBLIC" in sql
    assert "ALTER  SCHEMA public OWNER TO" in sql or "ALTER SCHEMA public OWNER TO" in sql


def test_la_reapplication_des_acl_nest_pas_optionnelle(instantane, tmp_path):
    """Aucun drapeau ne permet de la sauter : il n'y a pas de cas légitime où
    l'on restaure une base de locataire sans son isolation."""
    import inspect

    signature = inspect.signature(restore)
    assert not any("acl" in nom.lower() for nom in signature.parameters)


# ─── refus ───────────────────────────────────────────────────────────────────


def test_un_dump_absent_est_un_refus(instantane, tmp_path):
    r = _cluster(base_existe=True)
    with pytest.raises(RestoreError, match="absent"):
        restore(Psql(r), r, instantane, database="wiki",
                ref="20260820-093240", pre_dir=tmp_path / "filets")
    assert _indice(r, "dropdb") == -1, "rien ne doit être détruit"


def test_un_role_absent_est_un_refus_qui_renvoie_aux_globals(instantane, tmp_path):
    """Les rôles se reposent AVANT les bases. Ce refus est le rappel."""
    r = _cluster(base_existe=True, role_existe=False)
    with pytest.raises(RestoreError, match="globals.sql"):
        restore(Psql(r), r, instantane, database="forge",
                ref="20260820-093240", pre_dir=tmp_path / "filets")
    assert _indice(r, "dropdb") == -1


def test_le_refus_survient_avant_toute_destruction(instantane, tmp_path):
    r = _cluster(base_existe=True, role_existe=False)
    with pytest.raises(RestoreError):
        restore(Psql(r), r, instantane, database="forge",
                ref="20260820-093240", pre_dir=tmp_path / "filets")
    assert _indice_sql(r, "pg_terminate_backend") == -1


def test_une_reference_incomprise_est_refusee(instantane, tmp_path):
    r = _cluster(base_existe=True)
    with pytest.raises(ValueError, match="attendu"):
        restore(Psql(r), r, instantane, database="forge", ref="hier",
                pre_dir=tmp_path / "filets")


# ─── détails d'invocation ────────────────────────────────────────────────────


def test_le_proprietaire_par_defaut_est_le_nom_de_la_base(instantane, tmp_path):
    """Convention du dépôt : un locataire, une base et un rôle du même nom."""
    r = _cluster(base_existe=False, proprietaire="")
    rapport = restore(Psql(r), r, instantane, database="forge",
                      ref="20260820-093240", pre_dir=tmp_path / "filets")
    assert rapport.owner == "forge"


def test_pg_restore_passe_le_role(instantane, tmp_path):
    """Sans `--role`, les tables appartiennent à postgres et le locataire ne
    peut plus rien en faire."""
    r = _cluster(base_existe=True, proprietaire="forge")
    restore(Psql(r), r, instantane, database="forge", ref="20260820-093240",
            pre_dir=tmp_path / "filets")
    argv = r.calls[_indice(r, "pg_restore")]
    assert "--role=forge" in argv
    assert "--no-owner" in argv


def test_la_base_est_recreee_avec_une_collation_binaire(instantane, tmp_path):
    """LC_COLLATE C : l'ordre ne dépend plus de la libc de la machine, et un
    index reste valide d'un hôte à l'autre."""
    r = _cluster(base_existe=False)
    restore(Psql(r), r, instantane, database="forge", ref="20260820-093240",
            pre_dir=tmp_path / "filets")
    argv = r.calls[_indice(r, "createdb")]
    assert "-T" in argv and "template0" in argv
    assert "--lc-collate" in argv and "C" in argv


def test_le_dump_du_filet_passe_par_un_fichier_pas_une_redirection(
    instantane, tmp_path
):
    """`pg_dump -f` plutôt qu'une redirection : aucun shell n'intervient, donc
    aucun échappement à faire sur un chemin."""
    r = _cluster(base_existe=True)
    restore(Psql(r), r, instantane, database="forge", ref="20260820-093240",
            pre_dir=tmp_path / "filets")
    argv = r.calls[_indice(r, "pg_dump")]
    assert "-f" in argv
    assert not any(">" in a for a in argv)


# ─── contrôle ────────────────────────────────────────────────────────────────


def test_verify_ne_fait_jamais_echouer(instantane):
    """Un avertissement n'est pas une panne : `verify` rapporte, il ne juge
    pas."""
    r = _cluster(base_existe=True)
    rapport = verify(Psql(r), database="forge", owner="forge")
    assert rapport.tables == 12


def test_verify_signale_que_public_peut_se_connecter():
    r = FakeRunner()
    r.when(lambda argv: any("datacl" in a for a in argv),
           Result(("psql",), 0, "\n", ""))
    r.when(lambda argv: any("FROM pg_tables" in a for a in argv),
           Result(("psql",), 0, "0\n", ""))
    rapport = verify(Psql(r), database="forge", owner="forge")
    assert rapport.public_can_connect is True


def test_verify_compare_au_proprietaire_reel_pas_au_nom_de_la_base():
    """Le bash comparait `tableowner <> '<nom de la base>'`. Une base dont le
    propriétaire porte un autre nom déclenchait donc un avertissement à chaque
    contrôle, même après une restauration parfaite."""
    r = FakeRunner()
    r.when(lambda argv: any("datacl" in a for a in argv),
           Result(("psql",), 0, "{forge=CTc/forge}\n", ""))
    r.when(lambda argv: any("tableowner" in a for a in argv),
           Result(("psql",), 0, "0\n", ""))
    r.when(lambda argv: any("FROM pg_tables" in a for a in argv),
           Result(("psql",), 0, "5\n", ""))
    verify(Psql(r), database="forge", owner="proprietaire_different")
    assert "proprietaire_different" in _sql_envoye(r)
