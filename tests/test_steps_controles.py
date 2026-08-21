"""Section C — les contrôles. Lecture seule, aucune action, que des constats.

Une étape de cette section ne propose JAMAIS d'action : elle regarde et elle
dit. C'est ce qui la rend sûre à jouer dans les trois modes, et c'est aussi
pourquoi elle est portée tôt — elle ne peut rien casser.
"""

from __future__ import annotations

import pytest

from core.converge import Mode
from core.runner import CommandError, FakeRunner, Result
from pgtool.deploy import Options, Paths, contexte
from pgtool.steps.controles import (
    HbaRules,
    SocketsEnEcoute,
    TimerHorsSite,
    TimerSauvegarde,
)

HBA_SAIN = (
    "15\x1flocal\x1fall\x1fpostgres\x1f\x1fpeer\x1f\n"
    "19\x1fhost\x1fall\x1fall\x1f127.0.0.1\x1fscram-sha-256\x1f\n"
    "34\x1fhost\x1fall\x1fall\x1f0.0.0.0\x1freject\x1f\n"
)

SS_DEUX_SOCKETS = (
    "LISTEN 0 200 0.0.0.0:5432 0.0.0.0:* users:((\"postgres\",pid=152,fd=6))\n"
    "LISTEN 0 200 [::]:5432 [::]:* users:((\"postgres\",pid=152,fd=7))\n"
)


@pytest.fixture
def ctx(tmp_path):
    return contexte(
        runner=FakeRunner(),
        paths=Paths(src=tmp_path / "depot" / "pve-eranikus" / "pgsql"),
        opts=Options(ctid=200),
        mode=Mode.STATUS,
    )


def _repond(ctx, fragment, sortie, code=0):
    ctx.runner.when(
        lambda argv, f=fragment: any(f in a for a in argv),
        Result(("x",), code, sortie, ""),
    )


# ─── pg_hba : ce qui est RÉELLEMENT chargé ───────────────────────────────────


def test_les_regles_chargees_sont_comptees(ctx):
    """Un `reload` réussi ne prouve pas que le fichier a été relu : c'est
    `pg_hba_file_rules` qui fait foi."""
    _repond(ctx, "pg_hba_file_rules", HBA_SAIN)
    resultat = HbaRules().check(ctx)
    assert resultat.state == "ok"
    assert "3 règle(s)" in resultat.detail


def test_une_regle_en_erreur_est_un_echec(ctx):
    """Une ligne mal formée est ignorée par PostgreSQL en silence — et
    l'accès qu'elle devait accorder n'existe pas."""
    mauvaise = HBA_SAIN + "40\x1fhost\x1fall\x1fall\x1fbidon\x1fscram\x1fadresse invalide\n"
    _repond(ctx, "pg_hba_file_rules", mauvaise)
    resultat = HbaRules().check(ctx)
    assert resultat.state == "error"
    assert "1" in resultat.detail


def test_un_cluster_muet_est_un_echec_pas_un_succes(ctx):
    """Si psql ne répond pas, on ne sait rien — et ne rien savoir n'est pas la
    même chose qu'aller bien."""
    ctx.runner.when(
        lambda argv: any("pg_hba_file_rules" in a for a in argv),
        Result(("psql",), 2, "", "connection refused"),
    )
    resultat = HbaRules().check(ctx)
    assert resultat.state == "error"
    assert "PostgreSQL" in resultat.detail


def test_un_controle_ne_propose_jamais_daction(ctx):
    _repond(ctx, "pg_hba_file_rules", HBA_SAIN)
    assert HbaRules().check(ctx).actions == ()


# ─── sockets ─────────────────────────────────────────────────────────────────


def test_deux_sockets_sont_attendues(ctx):
    """0.0.0.0 et [::]. Une seule veut dire qu'un `bind()` a échoué sans que
    PostgreSQL s'en émeuve : il se déclare démarré quand même."""
    _repond(ctx, "ss", SS_DEUX_SOCKETS)
    resultat = SocketsEnEcoute().check(ctx)
    assert resultat.state == "ok"
    assert "2" in resultat.detail


def test_une_seule_socket_est_le_piege_de_listen_addresses(ctx):
    """`SHOW listen_addresses` MENT : il renvoie ce que la configuration
    demande, pas ce que le processus a obtenu. Seul `ss` fait foi."""
    _repond(ctx, "ss", SS_DEUX_SOCKETS.splitlines()[0] + "\n")
    resultat = SocketsEnEcoute().check(ctx)
    assert resultat.state == "error"
    assert "listen_addresses" in resultat.detail


def test_aucune_socket_est_un_echec(ctx):
    _repond(ctx, "ss", "")
    assert SocketsEnEcoute().check(ctx).state == "error"


# ─── timers ──────────────────────────────────────────────────────────────────


def test_le_timer_de_sauvegarde_actif(ctx):
    resultat = TimerSauvegarde().check(ctx)     # FakeRunner répond 0 par défaut
    assert resultat.state == "ok"


def test_le_timer_de_sauvegarde_inactif_est_un_echec(ctx):
    ctx.runner.when(
        lambda argv: "is-enabled" in argv and "pg-backup.timer" in argv,
        Result(("systemctl",), 1, "", ""),
    )
    resultat = TimerSauvegarde().check(ctx)
    assert resultat.state == "error"
    assert "sans filet" in resultat.detail


def test_le_timer_hors_site_inactif_est_signale(ctx):
    """Le bash n'avait PAS de branche KO ici : un hors-site désarmé passait
    inaperçu au résumé, et personne ne s'apercevait que la copie ne partait
    plus."""
    ctx.runner.when(
        lambda argv: "is-enabled" in argv and "pgbk-offsite.timer" in argv,
        Result(("systemctl",), 1, "", ""),
    )
    assert TimerHorsSite().check(ctx).state == "error"


def test_le_timer_hors_site_est_saute_sans_hors_site(ctx):
    ctx.opts = Options(ctid=200, do_offsite=False)
    assert TimerHorsSite().skip_if(ctx) is not None


def test_le_timer_de_sauvegarde_est_lu_dans_le_conteneur(ctx):
    """Il vit dans le CT, pas sur le nœud : l'interroger depuis l'hôte
    répondrait sur la mauvaise machine."""
    TimerSauvegarde().check(ctx)
    assert any(argv[:2] == ("pct", "exec") for argv in ctx.runner.calls)


def test_le_timer_hors_site_est_lu_sur_le_noeud(ctx):
    """Lui vit sur l'hôte. C'est la confusion la plus facile à faire ici."""
    TimerHorsSite().check(ctx)
    assert not any(argv[:2] == ("pct", "exec") for argv in ctx.runner.calls)
