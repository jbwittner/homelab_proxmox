"""Section F — le hors-site, et les faits qui circulent entre étapes.

Le cœur de cette section n'est pas la copie de fichiers : c'est **la décision
d'armer ou non le timer**. Elle dépend de trois constats faits ailleurs, et le
bash s'y trompait — avec `--no-container`, la section A était sautée,
`MP2_STATE` restait « inconnu », la garde `== divergent` ne se déclenchait pas,
et le timer était armé sans que le volume ait jamais été vérifié.

Ici, un fait absent vaut « non déterminé » et **bloque l'armement**.
"""

from __future__ import annotations

import stat

import pytest

from core.converge import Mode
from core.runner import FakeRunner, Result
from pgtool.deploy import Options, Paths, contexte
from pgtool.steps.horssite import (
    ArmementHorsSite,
    CleGCP,
    SourceHorsSite,
    UniteHorsSite,
)

CONFIG_CT = (
    "arch: amd64\n"
    "protection: 1\n"
    "mp2: data:subvol-200-disk-0,mp=/var/backups/postgresql,backup=0\n"
)


@pytest.fixture
def ctx(tmp_path):
    service = tmp_path / "depot" / "pve-eranikus" / "pgsql"
    (service / "host").mkdir(parents=True)
    (service / "host" / "pgbk-offsite.sh").write_text("#!/bin/bash\n")
    (service / "host" / "pgbk-offsite.service").write_text("[Service]\n")
    (service / "host" / "pgbk-offsite.timer").write_text("[Timer]\n")
    cible = tmp_path / "cible"
    (cible / "etc" / "systemd" / "system").mkdir(parents=True)
    (cible / "usr" / "local" / "bin").mkdir(parents=True)

    c = contexte(
        runner=FakeRunner(),
        paths=Paths(src=service),
        opts=Options(ctid=200),
        mode=Mode.APPLY,
    )
    c.cible = cible
    return c


def _config(ctx, sortie=CONFIG_CT):
    ctx.runner.when(
        lambda argv: argv[:2] == ("pct", "config"),
        Result(("pct",), 0, sortie, ""),
    )


def _pvesm(ctx, chemin, code=0):
    ctx.runner.when(
        lambda argv: argv[:2] == ("pvesm", "path"),
        Result(("pvesm",), code, chemin + "\n", ""),
    )


# ─── la source : demandée à Proxmox, jamais devinée ──────────────────────────


def test_la_source_est_demandee_a_proxmox(ctx):
    """Déduire le chemin à la main marche jusqu'au jour où le pool change de
    nom. `pvesm path` répond pour n'importe quel CTID et n'importe quel pool."""
    _config(ctx)
    _pvesm(ctx, "/data/subvol-200-disk-0")
    resultat = SourceHorsSite().check(ctx)
    assert resultat.state == "ok"
    assert ctx.facts["offsite_src"] == "/data/subvol-200-disk-0"


def test_une_source_qui_ne_vise_pas_le_bon_ct_est_refusee(ctx):
    """Garde-fou : si le chemin résolu ne porte pas `subvol-<CTID>-`, on vise
    le volume d'un AUTRE conteneur. La copie hors-site partirait alors sur les
    sauvegardes de quelqu'un d'autre, dans un bucket qu'on ne peut pas purger.
    """
    _config(ctx)
    _pvesm(ctx, "/data/subvol-299-disk-0")
    resultat = SourceHorsSite().check(ctx)
    assert resultat.state == "error"
    assert "299" in resultat.detail or "200" in resultat.detail
    assert ctx.facts.get("offsite_src") is None, "un fait faux est pire qu'absent"


def test_une_source_non_resolue_ne_pose_pas_de_fait(ctx):
    _config(ctx)
    _pvesm(ctx, "", code=1)
    assert SourceHorsSite().check(ctx).state == "error"
    assert ctx.facts.get("offsite_src") is None


def test_un_ct_sans_mp2_est_refuse(ctx):
    _config(ctx, "arch: amd64\nprotection: 1\n")
    resultat = SourceHorsSite().check(ctx)
    assert resultat.state == "error"
    assert "mp2" in resultat.detail


# ─── la clé du compte de service ─────────────────────────────────────────────


def test_une_cle_absente_est_une_erreur_jamais_une_pose(ctx, tmp_path):
    """C'est un secret : il n'a rien à faire dans le dépôt, et le script ne
    peut pas le fabriquer. Il dit où le déposer et s'arrête là."""
    resultat = CleGCP(tmp_path / "absente.json").check(ctx)
    assert resultat.state == "error"
    assert resultat.actions == ()
    assert "OpenBao" in resultat.detail or "secret" in resultat.detail
    assert ctx.facts.get("gcp_key_ok") is False


def test_une_cle_vide_est_traitee_comme_absente(ctx, tmp_path):
    cle = tmp_path / "vide.json"
    cle.write_text("")
    assert CleGCP(cle).check(ctx).state == "error"


def test_une_cle_trop_ouverte_est_a_corriger(ctx, tmp_path):
    """Le mode se corrige, lui : c'est la seule chose qu'on puisse faire sans
    connaître le secret."""
    cle = tmp_path / "cle.json"
    cle.write_text('{"private_key":"x"}')
    cle.chmod(0o644)
    resultat = CleGCP(cle).check(ctx)
    assert resultat.state == "drift"
    assert any("chmod" in a.label for a in resultat.actions)
    for action in resultat.actions:
        action.run(ctx)
    assert stat.S_IMODE(cle.stat().st_mode) == 0o600


def test_une_cle_conforme_pose_le_fait(ctx, tmp_path):
    cle = tmp_path / "cle.json"
    cle.write_text('{"private_key":"x"}')
    cle.chmod(0o600)
    assert CleGCP(cle).check(ctx).state == "ok"
    assert ctx.facts["gcp_key_ok"] is True


# ─── l'armement : trois conditions, et aucune supposition ───────────────────


def _pret(ctx):
    ctx.facts.update(offsite_src="/data/subvol-200-disk-0",
                     gcp_key_ok=True, rclone_ok=True, mp2_state="ok")


def test_tout_est_pret_donc_on_arme(ctx):
    _pret(ctx)
    ctx.runner.when(lambda argv: "is-enabled" in argv, Result(("x",), 1, "", ""))
    resultat = ArmementHorsSite().check(ctx)
    assert resultat.state == "absent"
    assert any("enable" in a.label for a in resultat.actions)


def test_deja_arme_ne_propose_rien(ctx):
    _pret(ctx)
    assert ArmementHorsSite().check(ctx).state == "ok"


def test_un_fait_ABSENT_bloque_larmement(ctx):
    """LE trou du bash. Avec --no-container, la section A ne tourne pas et
    l'état de mp2 n'est jamais établi. Le bash comparait « == divergent », ce
    qui était faux pour « inconnu », et armait quand même.

    Ici l'absence de fait vaut « non déterminé », pas « tout va bien ».
    """
    _pret(ctx)
    del ctx.facts["mp2_state"]
    ctx.runner.when(lambda argv: "is-enabled" in argv, Result(("x",), 1, "", ""))
    resultat = ArmementHorsSite().check(ctx)
    assert resultat.state == "error"
    assert resultat.actions == (), "ne jamais armer sur une supposition"
    assert "mp2" in resultat.detail


def test_un_mp2_divergent_bloque_larmement(ctx):
    _pret(ctx)
    ctx.facts["mp2_state"] = "divergent"
    ctx.runner.when(lambda argv: "is-enabled" in argv, Result(("x",), 1, "", ""))
    resultat = ArmementHorsSite().check(ctx)
    assert resultat.state == "error"
    assert resultat.actions == ()


def test_une_cle_manquante_bloque_larmement(ctx):
    _pret(ctx)
    ctx.facts["gcp_key_ok"] = False
    ctx.runner.when(lambda argv: "is-enabled" in argv, Result(("x",), 1, "", ""))
    assert ArmementHorsSite().check(ctx).actions == ()


def test_un_timer_deja_actif_mais_sans_prerequis_est_signale(ctx):
    """Cas vicieux : le timer tourne, mais un prérequis a disparu depuis. La
    copie échouera à 3h30 et personne ne regarde à 3h30."""
    _pret(ctx)
    ctx.facts["gcp_key_ok"] = False
    resultat = ArmementHorsSite().check(ctx)
    assert resultat.state == "error"
    assert "échouera" in resultat.detail


def test_larmement_est_saute_sans_hors_site(ctx):
    ctx.opts = Options(ctid=200, do_offsite=False)
    assert ArmementHorsSite().skip_if(ctx) is not None


# ─── les unités ──────────────────────────────────────────────────────────────


def test_une_unite_posee_demande_un_rechargement(ctx):
    """Trois unités posées ne doivent provoquer qu'un seul `daemon-reload` :
    l'action DÉCLARE l'effet, le parcours coalesce."""
    etape = UniteHorsSite("pgbk-offsite.service",
                          ctx.cible / "etc/systemd/system/pgbk-offsite.service")
    resultat = etape.check(ctx)
    assert resultat.state == "absent"
    assert "host.daemon-reload" in resultat.actions[0].effects


def test_le_script_hors_site_est_pose_en_755(ctx):
    etape = UniteHorsSite("pgbk-offsite.sh",
                          ctx.cible / "usr/local/bin/pgbk-offsite",
                          mode=0o755)
    for action in etape.check(ctx).actions:
        action.run(ctx)
    cible = ctx.cible / "usr/local/bin/pgbk-offsite"
    assert stat.S_IMODE(cible.stat().st_mode) == 0o755


# ─── ce que l'unité du dépôt déclare ─────────────────────────────────────────


UNITE = """[Unit]
Description=Copie hors-site

[Service]
Environment=PGBK_OFFSITE_RCLONE=/usr/bin/rclone
Environment=PGBK_OFFSITE_REMOTE=gcs
Environment=PGBK_OFFSITE_CONFIG=/root/.config/rclone/rclone.conf
"""


def test_unit_env_lit_la_copie_du_DEPOT(tmp_path):
    """Ni l'unité installée ni le drop-in : le dépôt fait foi, c'est ce que le
    déploiement va poser. Interroger `systemctl show` répondrait sur l'état
    d'avant, celui qu'on est justement en train de remplacer."""
    from pgtool.steps.horssite import unit_env

    unite = tmp_path / "pgbk-offsite.service"
    unite.write_text(UNITE)
    assert unit_env(unite, "PGBK_OFFSITE_REMOTE", "autre") == "gcs"


def test_unit_env_se_rabat_sur_le_defaut(tmp_path):
    """Une ligne Environment retirée ne doit pas produire un contrôle sur une
    chaîne vide, qui passerait pour un fichier absent."""
    from pgtool.steps.horssite import unit_env

    unite = tmp_path / "pgbk-offsite.service"
    unite.write_text(UNITE)
    assert unit_env(unite, "ABSENTE", "/par/defaut") == "/par/defaut"
    assert unit_env(tmp_path / "rien.service", "X", "d") == "d"


def test_unit_env_garde_la_derniere_occurrence(tmp_path):
    """systemd retient la dernière ; lire la première divergerait de ce que
    l'unité fait réellement."""
    from pgtool.steps.horssite import unit_env

    unite = tmp_path / "u.service"
    unite.write_text(UNITE + "Environment=PGBK_OFFSITE_REMOTE=gcs2\n")
    assert unit_env(unite, "PGBK_OFFSITE_REMOTE", "") == "gcs2"


# ─── rclone.conf ─────────────────────────────────────────────────────────────


def test_un_rclone_conf_absent_est_ecrit(ctx, tmp_path):
    from pgtool.steps.horssite import ConfigRclone

    conf = tmp_path / "rclone" / "rclone.conf"
    plan = ConfigRclone(conf, remote="gcs", cle=tmp_path / "k.json").check(ctx)
    assert plan.state == "absent"
    for action in plan.actions:
        action.run(ctx)
    ecrit = conf.read_text()
    assert "[gcs]" in ecrit
    assert "bucket_policy_only = true" in ecrit
    assert stat.S_IMODE(conf.stat().st_mode) == 0o600


def test_un_rclone_conf_present_nest_JAMAIS_reecrit(ctx, tmp_path):
    """Le fichier est hors dépôt et peut porter d'autres remotes : le réécrire
    en emporterait."""
    from pgtool.steps.horssite import ConfigRclone

    conf = tmp_path / "rclone.conf"
    conf.write_text("[autre]\ntype = s3\n")
    plan = ConfigRclone(conf, remote="gcs", cle=tmp_path / "k.json").check(ctx)
    assert plan.actions == (), "aucune action : on n'y touche pas"
    assert conf.read_text() == "[autre]\ntype = s3\n"


def test_bucket_policy_only_manquant_est_DICTE_pas_ajoute(ctx, tmp_path):
    """Sans lui, UBLA refuse chaque insertion en 400 et zéro octet est écrit.
    La ligne exacte est donnée ; l'ajouter serait éditer un fichier qu'on ne
    possède pas."""
    from pgtool.steps.horssite import ConfigRclone

    conf = tmp_path / "rclone.conf"
    conf.write_text("[gcs]\ntype = google cloud storage\n")
    plan = ConfigRclone(conf, remote="gcs", cle=tmp_path / "k.json").check(ctx)
    assert plan.state == "error"
    assert plan.actions == ()
    assert "bucket_policy_only = true" in plan.detail


# ─── le drop-in du nœud ──────────────────────────────────────────────────────


def test_le_dropin_porte_CE_noeud_et_CE_volume(ctx, tmp_path):
    """L'unité du dépôt ne porte que des valeurs par défaut lisibles ; c'est le
    drop-in qui rend le hors-site juste sur --ctid 201 comme sur un autre
    nœud, sans rien éditer dans le dépôt."""
    from pgtool.steps.horssite import DropInNoeud

    ctx.facts["offsite_src"] = "/data/subvol-200-disk-0"
    cible = tmp_path / "10-noeud.conf"
    plan = DropInNoeud(cible, node="pve-eranikus").check(ctx)
    assert plan.state == "absent"
    for action in plan.actions:
        action.run(ctx)
    ecrit = cible.read_text()
    assert "Environment=PGBK_OFFSITE_NODE=pve-eranikus" in ecrit
    assert "Environment=PGBK_OFFSITE_SRC=/data/subvol-200-disk-0" in ecrit


def test_un_dropin_a_jour_ne_propose_rien(ctx, tmp_path):
    from pgtool.steps.horssite import DropInNoeud

    ctx.facts["offsite_src"] = "/data/subvol-200-disk-0"
    cible = tmp_path / "10-noeud.conf"
    etape = DropInNoeud(cible, node="pve-eranikus")
    for action in etape.check(ctx).actions:
        action.run(ctx)
    assert etape.check(ctx).state == "ok"


def test_sans_source_resolue_le_dropin_nest_pas_ecrit(ctx, tmp_path):
    """Écrire une source qu'on n'a pas su résoudre ferait partir la copie de
    3h30 sur un chemin inventé."""
    from pgtool.steps.horssite import DropInNoeud

    plan = DropInNoeud(tmp_path / "10-noeud.conf", node="n").check(ctx)
    assert plan.state == "error"
    assert plan.actions == ()


def test_reecrire_le_dropin_demande_un_daemon_reload(ctx, tmp_path):
    from pgtool.steps.horssite import EFFET_RELOAD, DropInNoeud

    ctx.facts["offsite_src"] = "/data/subvol-200-disk-0"
    plan = DropInNoeud(tmp_path / "10-noeud.conf", node="n").check(ctx)
    assert EFFET_RELOAD in plan.actions[0].effects
