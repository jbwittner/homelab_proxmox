"""`pg deploy` : les trois modes, et ce qu'ils promettent.

Le contrat de cette commande tient en une phrase : **`--status` et `--dry-run`
ne modifient rien**. Ces tests le vérifient sur le seul chemin qui compte —
celui où le Runner écrirait vraiment.
"""

from __future__ import annotations

import pytest

from pgtool.cli import build_parser


def _args(*argv):
    return build_parser().parse_args(list(argv))


def test_status_et_dry_run_sont_deux_modes_distincts():
    """`--status` constate, `--dry-run` constate ET annonce ce qu'il ferait.
    Les confondre ferait passer le premier pour un plan."""
    from core.converge import Mode
    from pgtool.cli import _mode_de

    assert _mode_de(_args("deploy", "--status")) is Mode.STATUS
    assert _mode_de(_args("deploy", "--dry-run")) is Mode.DRY_RUN
    assert _mode_de(_args("deploy")) is Mode.APPLY


def test_les_drapeaux_de_pg_deploy_sh_sont_tous_portes():
    """Une bascule franche : la ligne de commande d'hier doit encore marcher,
    sinon les procédures du runbook mentent."""
    args = _args(
        "deploy", "--dry-run", "--restart", "--no-container", "--no-offsite",
        "--no-install", "--no-first-run", "--admin", "jb", "--tenant", "forgejo",
    )
    from pgtool.cli import _options_de

    opts = _options_de(args, ctid=200)
    assert opts.ctid == 200
    assert opts.force_restart is True
    assert opts.do_container is False
    assert opts.do_offsite is False
    assert opts.do_install is False
    assert opts.do_first_run is False
    assert opts.admin == "jb"
    assert opts.tenant == "forgejo"


def test_les_secrets_ne_sont_autorises_que_sur_demande():
    """Rejouer un déploiement de routine ne doit pas faire apparaître un mot de
    passe : c'est `--admin` ou `--tenant` qui l'autorise, rien d'autre."""
    from pgtool.cli import _options_de, _secrets_autorises

    assert _secrets_autorises(_options_de(_args("deploy"), ctid=200)) is False
    assert _secrets_autorises(
        _options_de(_args("deploy", "--admin", "jb"), ctid=200)) is True
    assert _secrets_autorises(
        _options_de(_args("deploy", "--tenant", "f"), ctid=200)) is True


@pytest.mark.parametrize("drapeau", ["--status", "--dry-run"])
def test_ni_status_ni_dry_run_nECRIVENT_quoi_que_ce_soit(drapeau, tmp_path):
    """Le filet du moteur : en simulation le Runner est neutralisé, donc une
    écriture égarée dans un `check()` mal écrit ne coûte rien. Ce test le
    constate sur le contexte réellement construit par la commande."""
    from core.runner import FakeRunner
    from pgtool.cli import _contexte_deploy

    ctx = _contexte_deploy(
        _args("deploy", drapeau), ctid=200, runner=FakeRunner(),
        src=tmp_path / "depot" / "pve-eranikus" / "pgsql",
    )
    assert ctx.runner.dry_run is True
    assert ctx.fs.dry_run is True


def test_en_mode_reel_rien_nest_neutralise(tmp_path):
    from core.runner import FakeRunner
    from pgtool.cli import _contexte_deploy

    ctx = _contexte_deploy(
        _args("deploy"), ctid=200, runner=FakeRunner(),
        src=tmp_path / "depot" / "pve-eranikus" / "pgsql",
    )
    assert ctx.runner.dry_run is False


def test_un_KO_au_bilan_sort_en_1():
    """Le code de retour est ce que systemd et les habitudes lisent : un
    déploiement dont une étape a échoué ne peut pas sortir en 0."""
    from core.converge import Report
    from pgtool.cli import _code_de_sortie

    assert _code_de_sortie([Report("a", "A", "ok")]) == 0
    assert _code_de_sortie([Report("a", "A", "drift")]) == 0
    assert _code_de_sortie([Report("a", "A", "ok"),
                            Report("b", "B", "error")]) == 1
    assert _code_de_sortie([Report("b", "B", "unknown")]) == 1


def test_une_etape_bloquee_par_un_secret_ne_fait_pas_echouer_le_deploiement():
    """« Bloquée » veut dire « non demandée », pas « en panne ». Sortir en 1
    ferait passer un déploiement de routine pour un incident."""
    from core.converge import Report
    from pgtool.cli import _code_de_sortie

    assert _code_de_sortie([Report("admin", "G", "blocked")]) == 0
