"""Les retraits — et surtout : ne proposer que des actions qui font quelque chose.

DÉFAUT CONSTATÉ LE 21 AOÛT 2026, dans un `fj deploy --dry-run` réel sur
`pve-eranikus` :

    [dry-run] rm /etc/systemd/system/fjbk-offsite.timer
    [dry-run] systemctl disable --now fjbk-offsite.service
    [dry-run] rm /etc/systemd/system/fjbk-offsite.service

Le timer n'était pas armé — correct, il ne l'avait jamais été. Mais le
**service** était annoncé comme à désarmer, alors qu'il n'a pas de section
`[Install]` : il est `static`, et un `systemctl disable` sur une unité statique
ne fait rien du tout.

`systemctl is-enabled` répond `static` en sortant **0**, ce qu'un test de code
de retour lit comme « armé ». D'où une action proposée dans le plan qui
n'aurait rien changé — et « le plan est LA description du delta » n'admet pas
d'action qui ne délègue rien.
"""

from __future__ import annotations

import pytest

from core.converge import Mode
from core.runner import FakeRunner, Result
from fjtool.deploy import Options, Paths, contexte
from fjtool.steps import retraits as H


@pytest.fixture
def ctx(depot_forgejo, tmp_path):
    return contexte(
        runner=FakeRunner(),
        paths=Paths(src=depot_forgejo),
        opts=Options(ctid=400),
        mode=Mode.STATUS,
    )


def _etat_unite(ctx, valeur: str):
    """Ce que `systemctl show -p UnitFileState --value` rend."""
    ctx.runner.when("UnitFileState", Result(("systemctl",), 0, valeur + "\n", ""))


def test_une_unite_static_nest_pas_desarmee(ctx, tmp_path):
    """LE défaut. `static` veut dire « aucune section [Install] » : il n'y a
    rien à désarmer, et le proposer serait annoncer une action sans effet."""
    chemin = tmp_path / "fjbk-offsite.service"
    chemin.write_text("[Unit]\n")
    _etat_unite(ctx, "static")

    resultat = H.RetraitUniteArmee(
        "fjbk-offsite.service", chemin, motif="plus d'objet"
    ).check(ctx)

    libelles = " ".join(a.label for a in resultat.actions)
    assert "disable" not in libelles, "une unité statique n'a rien à désarmer"
    assert "rm" in libelles, "elle doit tout de même être retirée"


def test_une_unite_armee_est_desarmee_avant_dtre_retiree(ctx, tmp_path):
    """L'ordre n'est pas décoratif : retirer le fichier d'une unité armée
    laisse un lien pendant dans `*.wants/`."""
    chemin = tmp_path / "fjbk-offsite.timer"
    chemin.write_text("[Timer]\n")
    _etat_unite(ctx, "enabled")

    resultat = H.RetraitUniteArmee(
        "fjbk-offsite.timer", chemin, motif="plus d'objet"
    ).check(ctx)

    libelles = [a.label for a in resultat.actions]
    assert len(libelles) == 2
    assert "disable" in libelles[0]
    assert libelles[1].startswith("rm ")


def test_une_unite_absente_et_desarmee_ne_propose_rien(ctx, tmp_path):
    """« Zéro modification sur un état conforme » vaut aussi pour ce qui n'est
    plus là : un retrait déjà fait ne se redit pas."""
    _etat_unite(ctx, "")

    resultat = H.RetraitUniteArmee(
        "fjbk-offsite.timer", tmp_path / "absent", motif="plus d'objet"
    ).check(ctx)

    assert resultat.state == "ok"
    assert not resultat.actions


def test_un_fichier_present_mais_desarme_est_seulement_retire(ctx, tmp_path):
    chemin = tmp_path / "fjbk-offsite.timer"
    chemin.write_text("[Timer]\n")
    _etat_unite(ctx, "disabled")

    resultat = H.RetraitUniteArmee(
        "fjbk-offsite.timer", chemin, motif="plus d'objet"
    ).check(ctx)

    libelles = [a.label for a in resultat.actions]
    assert len(libelles) == 1
    assert libelles[0].startswith("rm ")


def test_le_motif_apparait_dans_le_verdict(ctx, tmp_path):
    """Un retrait qui ne dit pas POURQUOI se lit comme une perte de fonction,
    et ne se relit pas dans six mois."""
    chemin = tmp_path / "fjbk-offsite.timer"
    chemin.write_text("[Timer]\n")
    _etat_unite(ctx, "disabled")

    resultat = H.RetraitUniteArmee(
        "fjbk-offsite.timer", chemin, motif="la base est un locataire du CT 200"
    ).check(ctx)
    assert "locataire du CT 200" in resultat.detail
