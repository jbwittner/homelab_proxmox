"""La ligne de commande — ce qu'elle accepte, et ce qu'elle rend au shell.

Deux défauts constatés sur `pg` le 21 août 2026 sont défendus ici, parce qu'ils
se reproduiraient à l'identique sans test :

  - **`--ctid` n'existait que sur le parseur global**, alors que la forme
    écrite partout dans la documentation est `pg deploy --ctid 299`. Une
    relecture ne l'avait pas vu, parce qu'on lit ce qu'on croit avoir écrit ;
  - **une faute de frappe sortait en 2**, code qui veut dire « transfert en
    échec » dans la table du hors-site. Trois semaines plus tard, systemd
    l'aurait consignée comme une panne de copie.
"""

from __future__ import annotations

import argparse

import pytest

from fjtool import cli
from fjtool.location import Refus


def _analyser(*argv):
    return cli.construire_parseur().parse_args(argv)


# ─── --ctid, des deux côtés de la sous-commande ──────────────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        ("deploy", "--ctid", "400"),
        ("--ctid", "400", "deploy"),
        ("status", "--ctid", "400"),
        ("version", "--ctid", "400"),
        ("key", "--ctid", "400"),
    ],
)
def test_le_ctid_est_accepte_avant_comme_apres_la_sous_commande(argv):
    """La forme documentée doit exister pour de vrai.

    C'est ce contrôle-là qui a trouvé, côté `pg`, que `pg deploy --ctid 299`
    — la forme dont l'exercice de PRA dépend — n'existait pas.
    """
    args = _analyser(*argv)
    assert args.ctid == "400"


def test_le_ctid_local_gagne_sur_le_global():
    """Deux options du même nom : argparse garde la dernière analysée. Ce
    test fige le comportement plutôt que de le découvrir un jour de reprise."""
    args = _analyser("--ctid", "200", "deploy", "--ctid", "400")
    assert args.ctid == "400"


# ─── les codes de retour ─────────────────────────────────────────────────────


def test_une_faute_de_frappe_sort_en_un_et_non_en_deux():
    """2 veut dire « au moins un transfert a échoué ». Une commande mal tapée
    n'est pas une panne de copie."""
    with pytest.raises(SystemExit) as sortie:
        _analyser("deploy", "--foo")
    assert sortie.value.code == 1


def test_une_sous_commande_inconnue_sort_en_un():
    with pytest.raises(SystemExit) as sortie:
        _analyser("deploiement")
    assert sortie.value.code == 1


def test_l_aide_sort_en_zero():
    """`--help` n'est pas une erreur, et un timer qui l'invoquerait par
    accident ne doit pas être consigné comme une panne."""
    with pytest.raises(SystemExit) as sortie:
        _analyser("--help")
    assert sortie.value.code == 0


def test_aucune_sous_commande_est_un_usage_fautif():
    with pytest.raises(SystemExit) as sortie:
        _analyser()
    assert sortie.value.code == 1


# ─── le mode du déploiement ──────────────────────────────────────────────────


def _contexte(depot_forgejo, *argv):
    from core.runner import FakeRunner

    args = _analyser("deploy", *argv)
    return cli._contexte_deploy(
        args, ctid=400, runner=FakeRunner(), src=depot_forgejo
    )


def test_par_defaut_le_mode_applique(depot_forgejo):
    from core.converge import Mode

    assert _contexte(depot_forgejo).mode is Mode.APPLY


def test_status_et_dry_run_donnent_leurs_modes(depot_forgejo):
    from core.converge import Mode

    assert _contexte(depot_forgejo, "--status").mode is Mode.STATUS
    assert _contexte(depot_forgejo, "--dry-run").mode is Mode.DRY_RUN


def test_status_l_emporte_sur_dry_run(depot_forgejo):
    """Les deux demandent « ne touche à rien » ; le plus prudent gagne."""
    from core.converge import Mode

    ctx = _contexte(depot_forgejo, "--status", "--dry-run")
    assert ctx.mode is Mode.STATUS


def test_en_simulation_le_runner_est_neutralise(depot_forgejo):
    """Le filet : une écriture égarée dans un `check()` mal écrit est
    neutralisée plutôt qu'exécutée."""
    ctx = _contexte(depot_forgejo, "--dry-run")
    assert ctx.runner.dry_run is True
    assert ctx.fs.dry_run is True


def test_en_mode_appliquant_le_runner_ecrit(depot_forgejo):
    ctx = _contexte(depot_forgejo)
    assert ctx.runner.dry_run is False


# ─── les secrets, derrière un drapeau explicite ──────────────────────────────


def test_aucun_secret_nest_genere_par_defaut(depot_forgejo):
    """Rejouer un déploiement de routine ne doit pas pouvoir régénérer
    `SECRET_KEY` — ce qui rendrait illisible tout ce que la base contient."""
    assert _contexte(depot_forgejo).allow_secrets is False


def test_le_drapeau_secrets_autorise_la_generation(depot_forgejo):
    assert _contexte(depot_forgejo, "--secrets").allow_secrets is True


def test_admin_autorise_aussi_la_generation(depot_forgejo):
    """Créer un compte fait apparaître un mot de passe : c'est un secret."""
    assert _contexte(depot_forgejo, "--admin", "jbwittner").allow_secrets is True
    assert _contexte(depot_forgejo, "--admin", "jbwittner").opts.admin == "jbwittner"


# ─── les drapeaux --no-* ─────────────────────────────────────────────────────


def test_les_drapeaux_no_desactivent_une_pose_pas_un_controle(depot_forgejo):
    """C'est ce qui permet à `--status` de rester complet quels que soient les
    drapeaux : ils ne portent que sur `Options`, jamais sur la liste
    d'étapes."""
    from fjtool import plan

    ctx = _contexte(depot_forgejo, "--no-install", "--no-container")
    assert ctx.opts.do_install is False
    assert ctx.opts.do_container is False
    # La liste d'étapes ne change pas : ce sont les étapes qui se déclarent
    # sautées, et le bilan les compte.
    complet = _contexte(depot_forgejo)
    assert len(plan.etapes(ctx)) == len(plan.etapes(complet))


# ─── la racine du dépôt ──────────────────────────────────────────────────────


def test_un_repertoire_qui_nest_pas_le_service_est_refuse(tmp_path):
    """`fj deploy` lit ce qu'il pose DANS le dépôt. Le laisser travailler
    depuis n'importe où poserait des fichiers venus d'ailleurs."""
    args = argparse.Namespace(src=str(tmp_path))
    with pytest.raises(Refus) as capture:
        cli._source_du_depot(args)
    assert "ct/app.ini" in str(capture.value)


def test_le_depot_reel_est_accepte(depot_forgejo):
    args = argparse.Namespace(src=str(depot_forgejo))
    assert cli._source_du_depot(args) == depot_forgejo


def test_sans_src_la_racine_se_deduit_du_module():
    """Jouer `fj deploy` sans rien préciser doit marcher depuis le dépôt."""
    args = argparse.Namespace(src=None)
    assert (cli._source_du_depot(args) / "ct" / "app.ini").is_file()


# ─── le code de sortie du bilan ──────────────────────────────────────────────


def test_un_bilan_sans_erreur_sort_en_zero():
    from core.converge import Report

    rapports = [
        Report("a", "A", "ok"),
        Report("b", "B", "drift"),
        Report("c", "C", "absent"),
    ]
    assert cli._code_de_sortie(rapports) == 0


def test_une_etape_non_evaluable_sort_en_un():
    """« Non évaluable » n'est pas « ça va » : c'est le cœur de la règle du
    parcours, et le code de retour doit la porter jusqu'au shell."""
    from core.converge import UNKNOWN, Report

    assert cli._code_de_sortie([Report("a", "A", UNKNOWN)]) == 1


def test_une_etape_bloquee_ne_fait_pas_echouer_le_deploiement():
    """Une étape à secret non autorisée est un CHOIX de l'opérateur, pas une
    panne : `fj deploy` sans `--secrets` doit rendre 0."""
    from core.converge import BLOCKED, Report

    assert cli._code_de_sortie([Report("a", "G", BLOCKED)]) == 0


# ─── fj est un outil de NŒUD, et rien d'autre ────────────────────────────────


def test_fj_refuse_de_tourner_dans_un_conteneur():
    """Aucune commande ne s'exécute plus dans le CT 400 depuis que la base est
    un locataire du CT 200. Lancé là-bas par habitude, `fj` doit le DIRE —
    sans ce refus, il échouerait sur un « pct: command not found » qui ne
    rattache rien à sa cause."""
    from core.runner import FakeRunner
    from fjtool.location import Refus, exiger_le_noeud

    class SansPct(FakeRunner):
        def which(self, binary):
            return None

    with pytest.raises(Refus) as capture:
        exiger_le_noeud(SansPct())
    assert "outil du NŒUD" in str(capture.value)


def test_fj_accepte_de_tourner_sur_le_noeud():
    from core.runner import FakeRunner
    from fjtool.location import exiger_le_noeud

    class AvecPct(FakeRunner):
        def which(self, binary):
            return "/usr/sbin/pct"

    exiger_le_noeud(AvecPct())  # ne lève pas
