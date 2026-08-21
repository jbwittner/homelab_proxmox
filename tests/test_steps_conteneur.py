"""Section B — la pose dans le conteneur.

Deux choses s'y jouent qui n'existaient nulle part ailleurs : la **sentinelle
du montage** — rien n'est posé tant que `/etc/pgsql-git` n'est pas visible — et
les **effets coalescés**, qui remplacent les drapeaux `changed` et `copied` que
le bash levait à la main.
"""

from __future__ import annotations

import pytest

from core.converge import Mode, traverse
from core.runner import FakeRunner, Result
from pgtool.deploy import Options, Paths, contexte
from pgtool.steps.conteneur import (
    ClusterDetecte,
    FichierCT,
    MontageVisible,
    PaquetCT,
    SymlinkConf,
    TimerSauvegardeArme,
)

CLUSTERS = "18 main 5432 online postgres /var/lib/postgresql/18/main\n"


@pytest.fixture
def ctx(tmp_path):
    service = tmp_path / "depot" / "pve-eranikus" / "pgsql"
    (service / "ct").mkdir(parents=True)
    for nom in ("pg-backup.sh", "pgbk.sh", "pg-backup.service",
                "pg-backup.timer", "10-homelab.conf", "pg_hba.conf"):
        (service / "ct" / nom).write_text(f"# {nom}\n")
    return contexte(
        runner=FakeRunner(),
        paths=Paths(src=service),
        opts=Options(ctid=200),
        mode=Mode.APPLY,
    )


def _repond(ctx, predicat, sortie="", code=0):
    ctx.runner.when(predicat, Result(("x",), code, sortie, ""))


# ─── la sentinelle du montage ────────────────────────────────────────────────


def test_le_montage_visible_est_le_prealable(ctx):
    """Un `mpN` n'est lu qu'au démarrage : tant que le conteneur n'a pas
    redémarré, `/etc/pgsql-git` est vide, et poser quoi que ce soit depuis
    là-dedans copierait du néant."""
    assert MontageVisible().check(ctx).state == "ok"


def test_un_montage_absent_est_une_erreur(ctx):
    _repond(ctx, lambda argv: "test" in argv, code=1)
    resultat = MontageVisible().check(ctx)
    assert resultat.state == "error"
    assert "démarrage" in resultat.detail


def test_rien_nest_pose_tant_que_le_montage_manque(ctx):
    """Toutes les étapes de la section en dépendent : le parcours les marque
    non évaluables plutôt que de les laisser conclure dans le vide."""
    _repond(ctx, lambda argv: "test" in argv, code=1)
    _repond(ctx, lambda argv: "pg_lsclusters" in argv, CLUSTERS)
    rapports = traverse(
        [MontageVisible(), ClusterDetecte()], ctx
    )
    assert rapports[0].state == "error"
    assert rapports[1].state == "unknown"


# ─── paquets du conteneur ────────────────────────────────────────────────────


def test_un_paquet_present_ne_propose_rien(ctx):
    assert PaquetCT("sudo", "/usr/bin/sudo").check(ctx).state == "ok"


def test_un_paquet_absent_est_a_installer(ctx):
    _repond(ctx, lambda argv: "test" in argv and "/usr/bin/sudo" in argv, code=1)
    resultat = PaquetCT("sudo", "/usr/bin/sudo").check(ctx)
    assert resultat.state == "absent"
    assert any("apt-get install" in a.label for a in resultat.actions)


def test_un_paquet_absent_sans_install_est_une_erreur(ctx):
    ctx.opts = Options(ctid=200, do_install=False)
    _repond(ctx, lambda argv: "test" in argv and "/usr/bin/sudo" in argv, code=1)
    resultat = PaquetCT("sudo", "/usr/bin/sudo").check(ctx)
    assert resultat.state == "error"
    assert resultat.actions == ()


def test_les_paquets_sinstallent_DANS_le_conteneur(ctx):
    _repond(ctx, lambda argv: "test" in argv and "/usr/bin/sudo" in argv, code=1)
    resultat = PaquetCT("sudo", "/usr/bin/sudo").check(ctx)
    for action in resultat.actions:
        action.run(ctx)
    installs = [a for a in ctx.runner.calls if "apt-get" in a]
    assert installs and installs[0][:2] == ("pct", "exec")


# ─── le cluster ──────────────────────────────────────────────────────────────


def test_le_cluster_est_decouvert_pas_code_en_dur(ctx):
    """`/etc/postgresql/18/main` deviendra `/19/main` à la prochaine majeure.
    Le déduire de `pg_lsclusters` évite d'y penser ce jour-là."""
    _repond(ctx, lambda argv: "pg_lsclusters" in argv, CLUSTERS)
    resultat = ClusterDetecte().check(ctx)
    assert resultat.state == "ok"
    assert ctx.facts["cluster_dir"] == "/etc/postgresql/18/main"


def test_aucun_cluster_est_une_erreur(ctx):
    _repond(ctx, lambda argv: "pg_lsclusters" in argv, "")
    assert ClusterDetecte().check(ctx).state == "error"


def test_plusieurs_clusters_est_une_erreur(ctx):
    """La cible serait ambiguë, et se tromper de cluster pose la configuration
    sur une base qui n'est pas la bonne."""
    deux = CLUSTERS + "17 main 5433 online postgres /var/lib/postgresql/17/main\n"
    _repond(ctx, lambda argv: "pg_lsclusters" in argv, deux)
    resultat = ClusterDetecte().check(ctx)
    assert resultat.state == "error"
    assert "ambig" in resultat.detail.lower()


# ─── symlinks de configuration ───────────────────────────────────────────────


def test_un_symlink_conforme_ne_propose_rien(ctx):
    ctx.facts["cluster_dir"] = "/etc/postgresql/18/main"
    _repond(ctx, lambda argv: "readlink" in argv, "/etc/pgsql-git/pg_hba.conf\n")
    assert SymlinkConf("pg_hba.conf").check(ctx).state == "ok"


def test_un_symlink_absent_est_a_poser(ctx):
    ctx.facts["cluster_dir"] = "/etc/postgresql/18/main"
    _repond(ctx, lambda argv: "readlink" in argv, "")
    resultat = SymlinkConf("pg_hba.conf").check(ctx)
    assert resultat.state == "absent"
    assert any("ln -sfn" in a.label for a in resultat.actions)


def test_reposer_un_symlink_demande_un_redemarrage_de_postgresql(ctx):
    """`listen_addresses` ne se relit qu'au redémarrage : un reload ne
    suffirait pas à la première pose."""
    ctx.facts["cluster_dir"] = "/etc/postgresql/18/main"
    _repond(ctx, lambda argv: "readlink" in argv, "")
    resultat = SymlinkConf("pg_hba.conf").check(ctx)
    assert "ct.postgresql.restart" in resultat.actions[0].effects


# ─── fichiers du conteneur ───────────────────────────────────────────────────


def test_un_fichier_du_ct_est_copie_pas_lie(ctx):
    """Le montage est en lecture seule et ne peut pas porter le bit
    d'exécution : d'où une copie, faite DEPUIS le montage, DANS le conteneur."""
    _repond(ctx, lambda argv: "sh" in argv, code=1)
    resultat = FichierCT("pg-backup.sh", "/usr/local/bin/pg-backup.sh", 0o755)
    plan = resultat.check(ctx)
    assert plan.state in ("absent", "drift")
    assert any("install -m 755" in a.label for a in plan.actions)


def test_poser_un_fichier_du_ct_demande_un_daemon_reload(ctx):
    _repond(ctx, lambda argv: "sh" in argv, code=1)
    plan = FichierCT("pg-backup.timer",
                     "/etc/systemd/system/pg-backup.timer", 0o644).check(ctx)
    assert "ct.daemon-reload" in plan.actions[0].effects


def test_trois_fichiers_ne_font_quun_seul_daemon_reload(ctx):
    """Le bash levait un drapeau `copied` à la main à chaque copie. Ici l'effet
    est déclaré, et le parcours le coalesce."""
    journal = []
    ctx.on_effect("ct.daemon-reload", lambda c: journal.append("reload"))
    _repond(ctx, lambda argv: "sh" in argv, code=1)
    traverse(
        [
            MontageVisible(),
            FichierCT("pg-backup.service", "/etc/systemd/system/pg-backup.service", 0o644),
            FichierCT("pg-backup.timer", "/etc/systemd/system/pg-backup.timer", 0o644),
            FichierCT("pg-backup.sh", "/usr/local/bin/pg-backup.sh", 0o755),
        ],
        ctx,
    )
    assert journal == ["reload"]


def test_aucun_fichier_touche_aucun_daemon_reload(ctx):
    journal = []
    ctx.on_effect("ct.daemon-reload", lambda c: journal.append("reload"))
    traverse([MontageVisible(),
              FichierCT("pg-backup.sh", "/usr/local/bin/pg-backup.sh", 0o755)], ctx)
    assert journal == []


# ─── le timer de sauvegarde ──────────────────────────────────────────────────


def test_le_timer_est_arme_sil_ne_lest_pas(ctx):
    _repond(ctx, lambda argv: "is-enabled" in argv, code=1)
    resultat = TimerSauvegardeArme().check(ctx)
    assert resultat.state == "absent"
    assert any("enable --now" in a.label for a in resultat.actions)


def test_un_timer_deja_arme_ne_propose_rien(ctx):
    assert TimerSauvegardeArme().check(ctx).state == "ok"
