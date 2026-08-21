"""Section H — retirer ce que plus rien n'appelle.

Supprimer un script du dépôt ne le retire pas du nœud. Le binaire installé y
reste, exécutable, périmé, et quelqu'un le rejouera dans un an — c'est le seul
scénario que cette section existe pour empêcher.

**Un retrait est conditionnel à ce qui l'a remplacé.** Retirer le script
hors-site alors que l'unité qui le remplace n'est pas conforme laisserait le
nœud sans aucune copie hors-site, et le timer échouerait chaque nuit à 3h30.
Le prérequis n'est pas une précaution de style : c'est ce qui distingue un
retrait d'une régression.
"""

from __future__ import annotations

import pytest

from core.converge import Mode, traverse
from core.runner import FakeRunner
from pgtool.deploy import Options, Paths, contexte
from pgtool.steps.retraits import RetraitOrphelin


@pytest.fixture
def ctx(tmp_path):
    service = tmp_path / "depot" / "pve-eranikus" / "pgsql"
    (service / "host").mkdir(parents=True)
    return contexte(
        runner=FakeRunner(),
        paths=Paths(src=service),
        opts=Options(ctid=200),
        mode=Mode.APPLY,
    )


def test_un_binaire_perime_est_retire(ctx, tmp_path):
    perime = tmp_path / "usr" / "local" / "bin" / "pgbk-offsite"
    perime.parent.mkdir(parents=True)
    perime.write_text("#!/bin/bash\n# l'ancien\n")

    etape = RetraitOrphelin(perime, remplace_par="pg offsite")
    plan = etape.check(ctx)
    assert plan.state == "drift"
    for action in plan.actions:
        action.run(ctx)
    assert not perime.exists()


def test_un_binaire_deja_retire_ne_propose_rien(ctx, tmp_path):
    """« Zéro modification sur un état conforme » vaut aussi pour un retrait :
    une fois fait, il ne se redit pas à chaque déploiement."""
    etape = RetraitOrphelin(tmp_path / "absent", remplace_par="pg offsite")
    plan = etape.check(ctx)
    assert plan.state == "ok"
    assert plan.actions == ()


def test_le_motif_nomme_CE_QUI_REMPLACE(ctx, tmp_path):
    """Un retrait sans son remplaçant se lit comme une perte de fonction. Le
    bilan doit dire par quoi, sinon il ne se relit pas dans six mois."""
    perime = tmp_path / "pgbk-offsite"
    perime.write_text("x")
    plan = RetraitOrphelin(perime, remplace_par="pg offsite").check(ctx)
    assert "pg offsite" in plan.detail


def test_un_retrait_nest_PAS_joue_si_son_remplacant_nest_pas_conforme(ctx, tmp_path):
    """Retirer le script hors-site alors que l'unité qui le remplace est en
    défaut laisserait le nœud sans aucune copie hors-site."""
    from core.converge import Outcome

    perime = tmp_path / "pgbk-offsite"
    perime.write_text("x")

    class UniteEnDefaut:
        name, section, requires = "pgbk-offsite.service", "F", ()

        def skip_if(self, c):
            return None

        def check(self, c):
            return Outcome("error", "absente du dépôt")

    rapports = traverse(
        [UniteEnDefaut(),
         RetraitOrphelin(perime, remplace_par="pg offsite",
                         requires=("pgbk-offsite.service",))],
        ctx,
    )
    assert rapports[1].state == "unknown"
    assert perime.exists(), "rien n'a été retiré tant que le remplaçant est en défaut"


def test_en_simulation_rien_nest_retire(ctx, tmp_path):
    perime = tmp_path / "pgbk-offsite"
    perime.write_text("x")
    ctx.fs.dry_run = True
    for action in RetraitOrphelin(perime, remplace_par="pg offsite").check(ctx).actions:
        action.run(ctx)
    assert perime.exists()
