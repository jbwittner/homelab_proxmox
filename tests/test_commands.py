"""Les particularités d'outil, traitées une fois — donc testées une fois."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from core import commands
from core.commands import Psql, Rclone, RcloneConfig, Systemd, ident
from core.runner import FakeRunner, Result, Secret

CFG = RcloneConfig(remote="gcs", bucket="un-bucket", binary="/usr/bin/rclone")


def _argv(runner: FakeRunner) -> str:
    return " ".join(runner.calls[-1])


# ─── rclone : ce que le bucket exige ─────────────────────────────────────────


def test_bucket_policy_only_sur_toutes_les_commandes():
    """Sans ce drapeau : « Error 400: Cannot insert legacy ACL for an object
    when uniform bucket-level access is enabled », zéro octet écrit. Constaté
    le 20 août 2026 à la première exécution réelle."""
    r = FakeRunner()
    rc = Rclone(r, CFG)
    rc.reachable()
    assert "--gcs-bucket-policy-only" in _argv(r)
    rc.list_files("gcs:b/x")
    assert "--gcs-bucket-policy-only" in _argv(r)
    rc.copy(Path("/tmp/x"), "gcs:b/x")
    assert "--gcs-bucket-policy-only" in _argv(r)
    rc.check(Path("/tmp/x"), "gcs:b/x")
    assert "--gcs-bucket-policy-only" in _argv(r)


def test_options_de_robustesse_presentes():
    r = FakeRunner()
    Rclone(r, CFG).list_files("gcs:b/x")
    argv = _argv(r)
    assert "--retries 3" in argv
    assert "--low-level-retries 3" in argv
    assert "--stats 0" in argv


def test_bwlimit_seulement_si_demande():
    r = FakeRunner()
    Rclone(r, CFG).list_files("gcs:b")
    assert "--bwlimit" not in _argv(r)

    r2 = FakeRunner()
    Rclone(r2, RcloneConfig(bucket="b", bwlimit="10M")).list_files("gcs:b")
    assert "--bwlimit 10M" in _argv(r2)


def test_copy_ignore_existing_et_jamais_sync():
    r = FakeRunner()
    Rclone(r, CFG).copy(Path("/data/snap"), "gcs:b/snap")
    argv = _argv(r)
    assert "copy" in argv and "--ignore-existing" in argv
    assert "sync" not in argv


def test_aucune_methode_de_suppression_ou_de_synchronisation():
    """L'absence EST la garantie : le compte de service n'a pas
    objects.delete, et une correction en boucle masquerait la seule anomalie
    que ce montage existe pour révéler. Ne pas les ajouter."""
    exposees = {n for n, _ in inspect.getmembers(Rclone, inspect.isfunction)}
    assert not exposees & {"sync", "delete", "purge", "deletefile", "rmdir", "move"}


def test_check_est_toujours_a_sens_unique():
    """Ce qui existe en trop à distance ne nous regarde pas."""
    r = FakeRunner()
    Rclone(r, CFG).check(Path("/data/snap"), "gcs:b/snap")
    assert "--one-way" in _argv(r)


def test_size_only_uniquement_en_mode_size():
    r = FakeRunner()
    Rclone(r, CFG).check(Path("/x"), "gcs:b/x")
    assert "--size-only" not in _argv(r)

    r2 = FakeRunner()
    Rclone(r2, RcloneConfig(bucket="b", check_mode="size")).check(Path("/x"), "gcs:b/x")
    assert "--size-only" in _argv(r2)


def test_check_rend_les_deux_flux():
    """rclone écrit son verdict sur stderr : n'en garder qu'un perdrait le
    motif de la divergence."""
    r = FakeRunner()
    r.when("check", Result(("rclone",), 1, "sur stdout", "sur stderr"))
    ok, sortie = Rclone(r, CFG).check(Path("/x"), "gcs:b/x")
    assert ok is False
    assert "sur stdout" in sortie and "sur stderr" in sortie


def test_reachable_liste_plutot_que_about():
    """Le compte de service est objectViewer : il n'a pas
    storage.buckets.get, et `rclone about` échouerait sur un bucket sain."""
    r = FakeRunner()
    Rclone(r, CFG).reachable()
    argv = _argv(r)
    assert "lsf" in argv and "about" not in argv


def test_copy_est_diffusee_et_sans_delai():
    """Un transfert de 40 min doit rester visible pendant qu'il tourne, et
    c'est TimeoutStartSec de l'unité qui l'encadre — pas un défaut de 300 s."""
    vus = {}

    class Espion(FakeRunner):
        def _dispatch(self, argv, *, check, stdin=None, timeout=-1, stream=False):
            vus.update(timeout=timeout, stream=stream)
            return Result(tuple(argv), 0, "", "")

    Rclone(Espion(), CFG).copy(Path("/x"), "gcs:b/x")
    assert vus["stream"] is True
    assert vus["timeout"] is None


def test_path_compose_le_prefixe_distant():
    assert Rclone(FakeRunner(), CFG).path("noeud", "pgsql") == "gcs:un-bucket/noeud/pgsql"


# ─── psql ────────────────────────────────────────────────────────────────────


def test_identifiant_cite_et_double_les_guillemets():
    assert ident("forgejo") == '"forgejo"'
    assert ident('a"b') == '"a""b"'


def test_toujours_on_error_stop_en_ecriture():
    """Sans lui, un CREATE ROLE en échec laisse passer le CREATE DATABASE et
    produit une base orpheline sans propriétaire."""
    r = FakeRunner()
    Psql(r).execute("CREATE ROLE x")
    assert "ON_ERROR_STOP=1" in _argv(r)


def test_run_file_passe_par_les_variables_psql():
    r = FakeRunner()
    Psql(r).run_file("/etc/pgsql-git/tenant.sql", name="forgejo")
    argv = _argv(r)
    assert "-v name=forgejo" in argv and "-f /etc/pgsql-git/tenant.sql" in argv


def test_run_file_masque_un_mot_de_passe():
    """Le couple clé=valeur hérite du secret, sinon le masquage tomberait au
    moment même où on en a besoin."""
    r = FakeRunner()
    res = Psql(r).run_file("/x.sql", name="forgejo", password=Secret("tr3s-secret"))
    assert "tr3s-secret" not in " ".join(res.argv)
    assert "***" in res.argv
    assert "-v name=forgejo" in " ".join(res.argv), "seul le secret est masqué"


def test_lecture_en_tuples_seuls_et_non_alignes():
    r = FakeRunner()
    Psql(r).scalar("SELECT 1")
    assert "-tA" in _argv(r)


def test_rows_decoupe_sur_un_separateur_improbable():
    """Un séparateur qui peut apparaître dans une valeur — la barre verticale,
    par exemple — découperait une ligne au mauvais endroit."""
    r = FakeRunner()
    r.when("psql", Result(("psql",), 0, "1\x1flocal\x1fall\n2\x1fhost\x1fa|b\n", ""))
    assert Psql(r).rows("SELECT 1") == [["1", "local", "all"], ["2", "host", "a|b"]]


def test_database_owner_absent_rend_none():
    r = FakeRunner()
    r.when("psql", Result(("psql",), 0, "\n", ""))
    assert Psql(r).database_owner("absente") is None


def test_psql_dans_le_conteneur_est_le_meme_code():
    """Même classe, même code métier : c'est le Runner qui décide où ça tourne.

    L'argv métier est identique des deux côtés ; seul le préfixe `pct exec`
    s'ajoute, et il vient de l'exécuteur, pas de `Psql`.
    """
    hote = FakeRunner()
    Psql(hote).scalar("SELECT 1")
    local = hote.calls[-1]

    dans_le_ct = FakeRunner()
    Psql(dans_le_ct.for_container(200)).scalar("SELECT 1")
    distant = dans_le_ct.calls[-1]

    assert distant[:5] == ("pct", "exec", "200", "--", "sudo")
    assert distant[4:] == local, "le code métier ne change pas"


# ─── systemd ─────────────────────────────────────────────────────────────────


def test_is_enabled_est_une_sonde():
    r = FakeRunner({"systemctl is-enabled --quiet t.timer": Result(("x",), 1, "", "")})
    assert Systemd(r).is_enabled("t.timer") is False


def test_journal_ne_leve_pas():
    """Consulter un journal absent ne doit pas faire échouer un diagnostic."""
    r = FakeRunner()
    r.when("journalctl", Result(("journalctl",), 1, "", "no entries"))
    assert Systemd(r).journal("absent.service") == []


# ─── frontière du paquet ─────────────────────────────────────────────────────


def test_core_ne_connait_pas_proxmox():
    """`pct` est l'affaire du nœud. Ce paquet est poussé DANS les conteneurs,
    où pct n'existe pas."""
    assert not hasattr(commands, "Pct")
    source = Path(commands.__file__).read_text(encoding="utf-8")
    assert "pct " not in source.replace("pct n'existe pas", "")


# ─── variables psql : jamais avec -c ─────────────────────────────────────────


def test_run_sql_envoie_le_sql_sur_lentree_standard():
    """psql ne substitue `:"var"` que sur l'entrée standard ou dans un fichier.
    Avec `-c`, la chaîne part telle quelle au serveur, qui répond :

        ERROR:  syntax error at or near ":"
        LINE 1: REVOKE CONNECT ON DATABASE :"cible" FROM PUBLIC;

    Constaté en production le 21 août 2026, pendant l'exercice de bascule, sur
    la réapplication des ACL — l'étape dont l'absence ne produit sinon aucun
    message.
    """
    r = FakeRunner()
    sql = 'REVOKE CONNECT ON DATABASE :"cible" FROM PUBLIC;'
    Psql(r).run_sql(sql, cible="forge")
    argv = r.calls[-1]
    assert "-c" not in argv, "avec -c, psql ne substitue rien"
    assert r.stdins[-1] == sql
    assert "-v" in argv and "cible=forge" in argv


def test_run_sql_choisit_la_base_de_connexion():
    """`db=` sélectionne la connexion ; les autres mots-clés sont des variables.
    Les confondre ferait jouer le SQL sur la mauvaise base."""
    r = FakeRunner()
    Psql(r).run_sql("SELECT 1", db="forge", proprietaire="forge")
    argv = r.calls[-1]
    assert "-d" in argv and argv[argv.index("-d") + 1] == "forge"
    assert "proprietaire=forge" in argv
    assert "db=forge" not in argv, "db n'est pas une variable psql"


def test_run_file_garde_le_fichier():
    """`-f` substitue les variables lui aussi : c'est ainsi que tenant.sql
    fonctionne depuis le début."""
    r = FakeRunner()
    Psql(r).run_file("/etc/pgsql-git/tenant.sql", name="forge")
    argv = r.calls[-1]
    assert "-f" in argv and argv[argv.index("-f") + 1] == "/etc/pgsql-git/tenant.sql"
    assert "-c" not in argv
