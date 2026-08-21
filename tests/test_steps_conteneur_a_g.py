"""Sections A et G — les points de montage, la protection, et les secrets.

La A est la plus délicate de tout le déploiement : elle touche à la protection
du conteneur et à ses disques, et elle est la seule à provoquer un
redémarrage. La G est celle qui fait apparaître des mots de passe.
"""

from __future__ import annotations

import pytest

from core.converge import Mode, traverse
from core.runner import FakeRunner, Result
from pgtool.deploy import Options, Paths, contexte
from pgtool.steps.prerequis import (
    ConteneurDemarre,
    Mp1Depot,
    Mp2Sauvegardes,
    Nesting,
    Protection,
    Startup,
)
from pgtool.steps.secrets import Locataire, PremiereSauvegarde, RoleAdmin

CONFIG = (
    "arch: amd64\n"
    "protection: 1\n"
    "features: nesting=1,keyctl=1\n"
    "mp1: /depot/pve-eranikus/pgsql/ct,mp=/etc/pgsql-git,ro=1\n"
    "mp2: data:subvol-200-disk-0,mp=/var/backups/postgresql,backup=0\n"
    "startup: order=1\n"
)


class FauxNoeud(FakeRunner):
    """Un nœud dont les lectures tiennent compte des écritures.

    Sans cela, `unprotected()` relirait une configuration figée et croirait
    n'avoir rien à rétablir.
    """

    def __init__(self, config=CONFIG):
        super().__init__()
        self.conf = {}
        for ligne in config.splitlines():
            cle, _, valeur = ligne.partition(":")
            if cle.strip():
                self.conf[cle.strip()] = valeur.strip()

    def _dispatch(self, argv, *, check, stdin=None, timeout=-1, stream=False):
        argv = tuple(argv)
        if argv[:2] == ("pct", "config"):
            self.calls.append(argv)
            rendu = "".join(f"{k}: {v}\n" for k, v in self.conf.items())
            return Result(argv, 0, rendu, "")
        if argv[:2] == ("pct", "set"):
            self.calls.append(argv)
            reste = list(argv[3:])
            while len(reste) >= 2:
                self.conf[reste[0].lstrip("-")] = reste[1]
                reste = reste[2:]
            return Result(argv, 0, "", "")
        return super()._dispatch(argv, check=check, stdin=stdin,
                                 timeout=timeout, stream=stream)


@pytest.fixture
def ctx(tmp_path):
    service = tmp_path / "depot" / "pve-eranikus" / "pgsql"
    (service / "ct").mkdir(parents=True)
    (service / "ct" / "tenant.sql").write_text("-- locataire\n")
    # La configuration factice doit porter le VRAI chemin du montage, sinon
    # mp1 serait toujours divergent pour une raison qui n'a rien à voir.
    config = CONFIG.replace("/depot/pve-eranikus/pgsql/ct", str(service / "ct"))
    return contexte(
        runner=FauxNoeud(config),
        paths=Paths(src=service),
        opts=Options(ctid=200),
        mode=Mode.APPLY,
    )


# ─── A. le conteneur doit tourner ────────────────────────────────────────────


def test_un_conteneur_demarre_ne_propose_rien(ctx):
    ctx.runner.when(lambda a: a[:2] == ("pct", "status"),
                    Result(("pct",), 0, "status: running\n", ""))
    assert ConteneurDemarre().check(ctx).state == "ok"


def test_un_conteneur_arrete_est_a_demarrer(ctx):
    ctx.runner.when(lambda a: a[:2] == ("pct", "status"),
                    Result(("pct",), 0, "status: stopped\n", ""))
    resultat = ConteneurDemarre().check(ctx)
    assert resultat.state == "absent"
    assert any("pct start" in a.label for a in resultat.actions)


# ─── A. nesting ──────────────────────────────────────────────────────────────


def test_nesting_present_ne_propose_rien(ctx):
    assert Nesting().check(ctx).state == "ok"


def test_nesting_absent_est_a_poser_et_demande_un_redemarrage(ctx):
    """Sans nesting=1, les unités qui montent un tmpfs pour les credentials
    systemd échouent en 243/CREDENTIALS — et le conteneur démarre en état
    dégradé sans que rien ne le signale."""
    ctx.runner.conf["features"] = "keyctl=1"
    resultat = Nesting().check(ctx)
    assert resultat.state == "absent"
    assert "ct.reboot" in resultat.actions[0].effects


def test_nesting_preserve_les_autres_features(ctx):
    ctx.runner.conf["features"] = "keyctl=1"
    for action in Nesting().check(ctx).actions:
        action.run(ctx)
    assert set(ctx.runner.conf["features"].split(",")) == {"nesting=1", "keyctl=1"}


def test_nesting_a_zero_est_remplace(ctx):
    ctx.runner.conf["features"] = "nesting=0,keyctl=1"
    assert Nesting().check(ctx).state == "drift"


# ─── A. les points de montage ────────────────────────────────────────────────


def test_mp1_conforme_ne_propose_rien(ctx, tmp_path):
    assert Mp1Depot().check(ctx).state == "ok"


def test_mp1_divergent_est_a_reposer(ctx):
    ctx.runner.conf["mp1"] = "/ailleurs,mp=/etc/pgsql-git,ro=1"
    resultat = Mp1Depot().check(ctx)
    assert resultat.state == "drift"
    assert "ct.reboot" in resultat.actions[0].effects


def test_mp1_est_insensible_a_lordre_des_options(ctx):
    """Proxmox réécrit la valeur : comparer les chaînes brutes conclurait à une
    divergence à chaque déploiement, donc à un conteneur redémarré pour rien."""
    source = ctx.paths.ct_src
    ctx.runner.conf["mp1"] = f"ro=1,{source},mp=/etc/pgsql-git"
    assert Mp1Depot().check(ctx).state == "ok"


def test_mp2_absent_est_a_creer(ctx):
    del ctx.runner.conf["mp2"]
    resultat = Mp2Sauvegardes().check(ctx)
    assert resultat.state == "absent"
    assert ctx.facts.get("mp2_state") is None, "rien n'est établi tant que rien n'est posé"


def test_mp2_conforme_pose_le_fait(ctx):
    assert Mp2Sauvegardes().check(ctx).state == "ok"
    assert ctx.facts["mp2_state"] == "ok"


def test_mp2_monte_ailleurs_nest_JAMAIS_touche(ctx):
    """On ne déplace pas un volume qui porte des données. Le signaler, et
    laisser le hors-site désarmé, vaut mieux que de « corriger »."""
    ctx.runner.conf["mp2"] = "data:subvol-200-disk-0,mp=/autre/chemin,backup=0"
    resultat = Mp2Sauvegardes().check(ctx)
    assert resultat.state == "error"
    assert resultat.actions == (), "aucune action : on n'y touche pas"
    assert ctx.facts["mp2_state"] == "divergent"


def test_mp2_sans_backup_zero_est_signale(ctx):
    """backup=0 tient 50 Go de dumps hors des vzdump du conteneur."""
    ctx.runner.conf["mp2"] = "data:subvol-200-disk-0,mp=/var/backups/postgresql"
    resultat = Mp2Sauvegardes().check(ctx)
    assert "backup=0" in resultat.detail


# ─── A. la protection ────────────────────────────────────────────────────────


def test_la_protection_est_levee_puis_remise(ctx):
    """La protection interdit toute modification de disque, ajout de point de
    montage compris. Ne pas la remettre ne produit aucune erreur."""
    ctx.runner.conf["mp1"] = "/ailleurs,mp=/etc/pgsql-git,ro=1"
    for action in Mp1Depot().check(ctx).actions:
        action.run(ctx)
    sets = [a for a in ctx.runner.calls if a[:2] == ("pct", "set")]
    assert "--protection" in sets[0] and sets[0][-1] == "0"
    assert "--protection" in sets[-1] and sets[-1][-1] == "1"


def test_la_protection_est_remise_meme_sur_exception(ctx):
    def echoue(c):
        with Protection(ctx.opts.ctid).levee(c):
            raise RuntimeError("boum")

    with pytest.raises(RuntimeError):
        echoue(ctx)
    sets = [a for a in ctx.runner.calls if a[:2] == ("pct", "set")]
    assert sets[-1][-1] == "1"


def test_un_conteneur_non_protege_est_signale(ctx):
    """Ce CT porte les données de tous les services : il devrait être protégé."""
    ctx.runner.conf["protection"] = "0"
    resultat = Protection(200).check(ctx)
    assert resultat.state == "drift"


# ─── A. startup ──────────────────────────────────────────────────────────────


def test_startup_deja_defini_ne_propose_rien(ctx):
    assert Startup().check(ctx).state == "ok"


def test_startup_absent_est_a_poser(ctx):
    del ctx.runner.conf["startup"]
    assert Startup().check(ctx).state == "absent"


# ─── G. secrets ──────────────────────────────────────────────────────────────


def test_la_premiere_sauvegarde_nest_declenchee_que_sil_ny_en_a_aucune(ctx):
    ctx.runner.when(lambda a: "find" in " ".join(a), Result(("x",), 0, "8\n", ""))
    assert PremiereSauvegarde().check(ctx).state == "ok"


def test_aucune_sauvegarde_est_a_declencher(ctx):
    ctx.runner.when(lambda a: "find" in " ".join(a), Result(("x",), 0, "0\n", ""))
    resultat = PremiereSauvegarde().check(ctx)
    assert resultat.state == "absent"
    assert any("pg-backup.service" in a.label for a in resultat.actions)


def test_sans_first_run_aucune_sauvegarde_est_une_erreur(ctx):
    """« Le conteneur reste sans filet » : on le dit, on ne le pose pas."""
    ctx.opts = Options(ctid=200, do_first_run=False)
    ctx.runner.when(lambda a: "find" in " ".join(a), Result(("x",), 0, "0\n", ""))
    resultat = PremiereSauvegarde().check(ctx)
    assert resultat.state == "error"
    assert resultat.actions == ()


def test_le_role_admin_est_saute_sans_demande(ctx):
    assert RoleAdmin().skip_if(ctx) is not None


def test_un_role_existant_nest_jamais_touche(ctx):
    """Rejouer un déploiement ne doit pas invalider un mot de passe déjà rangé
    dans OpenBao : aucune rotation par surprise."""
    ctx.opts = Options(ctid=200, admin="jbwittner")
    ctx.runner.when(lambda a: "pg_roles" in " ".join(a), Result(("x",), 0, "1\n", ""))
    resultat = RoleAdmin().check(ctx)
    assert resultat.state == "ok"
    assert resultat.actions == ()


def test_creer_un_role_est_une_action_a_secret(ctx):
    ctx.opts = Options(ctid=200, admin="jbwittner")
    ctx.runner.when(lambda a: "pg_roles" in " ".join(a), Result(("x",), 0, "\n", ""))
    resultat = RoleAdmin().check(ctx)
    assert resultat.state == "absent"
    assert resultat.actions[0].generates_secret is True


def test_une_action_a_secret_est_bloquee_par_defaut(ctx):
    """Le moteur refuse de la jouer sans demande explicite : un déploiement de
    routine ne doit pas faire apparaître un mot de passe."""
    ctx.opts = Options(ctid=200, admin="jbwittner")
    ctx.runner.when(lambda a: "pg_roles" in " ".join(a), Result(("x",), 0, "\n", ""))
    rapports = traverse([RoleAdmin()], ctx)
    assert rapports[0].state == "blocked"


def test_un_locataire_existant_nest_pas_recree(ctx):
    ctx.opts = Options(ctid=200, tenant="forgejo")
    ctx.runner.when(lambda a: "pg_database" in " ".join(a), Result(("x",), 0, "1\n", ""))
    assert Locataire().check(ctx).state == "ok"


def test_creer_un_locataire_passe_par_tenant_sql(ctx):
    """Le fichier est joué depuis le montage, avec des variables psql : c'est
    psql qui cite, donc un mot de passe peut contenir n'importe quoi."""
    ctx.opts = Options(ctid=200, tenant="forgejo")
    ctx.runner.when(lambda a: "pg_database" in " ".join(a), Result(("x",), 0, "\n", ""))
    resultat = Locataire().check(ctx)
    assert resultat.state == "absent"
    assert "tenant.sql" in resultat.actions[0].label
    assert resultat.actions[0].generates_secret is True
