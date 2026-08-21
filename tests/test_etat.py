"""`pg status` — la brique qui manquait le plus.

Le montage a trois maillons dont chacun peut se rompre en silence : la
sauvegarde locale, le timer qui la déclenche, la copie hors-site. Rien ne les
regardait ensemble, et c'est précisément le genre de panne qu'on découvre le
jour où l'on aurait eu besoin de restaurer.

Le constat est SÉPARÉ du rendu : le premier parle à l'infrastructure, le second
est une fonction pure de ce qu'il en a rapporté. C'est ce qui rend les alarmes
testables sans conteneur — et une alarme qu'on ne peut pas tester ne vaut rien.
"""

from __future__ import annotations

import pytest

from pgtool.etat import (
    AGE_ALARME_H,
    Etat,
    UniteEtat,
    alarmes,
    render_etat,
)


def _etat(**kw) -> Etat:
    """Un montage entièrement sain, que chaque test dégrade sur un point."""
    defauts = dict(
        ctid=200,
        ct_actif=True,
        sauvegardes=8,
        dernier="20260821-023014",
        age_heures=9,
        taille="1.2G",
        libre_mo=41200,
        timer_sauvegarde=UniteEtat("pg-backup.timer", True, True,
                                   "Sat 2026-08-22 02:30:00", "success"),
        timer_horssite=UniteEtat("pgbk-offsite.timer", True, True,
                                 "Sat 2026-08-22 03:30:00", "success"),
        distants=8,
        manquants=(),
    )
    defauts.update(kw)
    return Etat(**defauts)


def test_un_montage_sain_ne_leve_aucune_alarme():
    assert alarmes(_etat()) == []


def test_un_conteneur_arrete_est_une_alarme():
    """Tout le reste est alors du passé : ni sauvegarde, ni base servie."""
    assert alarmes(_etat(ct_actif=False))


def test_aucune_sauvegarde_est_lalarme_la_plus_grave():
    """Un cluster sans filet ne se découvre qu'au moment où l'on en aurait eu
    besoin. C'est la première ligne, pas la dernière."""
    dits = alarmes(_etat(sauvegardes=0, dernier="", age_heures=None))
    assert dits
    assert "sauvegarde" in dits[0].lower()


def test_une_sauvegarde_trop_vieille_est_une_alarme():
    """Le timer tourne à 2h30 : passé 26 h, une exécution a été manquée — et
    un timer « actif » qui échoue chaque nuit reste actif."""
    assert alarmes(_etat(age_heures=AGE_ALARME_H + 1))
    assert alarmes(_etat(age_heures=AGE_ALARME_H - 1)) == []


def test_un_age_inconnu_nest_pas_traite_comme_recent():
    """« Non déterminé » n'est pas « va bien ». Le confondre, c'est exactement
    le trou que MP2_STATE=inconnu ouvrait dans le bash."""
    assert alarmes(_etat(age_heures=None, dernier="20260101-000000"))


def test_un_timer_desarme_est_une_alarme():
    assert alarmes(_etat(
        timer_sauvegarde=UniteEtat("pg-backup.timer", False, False, "", "")))
    assert alarmes(_etat(
        timer_horssite=UniteEtat("pgbk-offsite.timer", False, False, "", "")))


def test_un_timer_arme_dont_la_derniere_execution_a_echoue_est_une_alarme():
    """Un timer armé qui échoue toutes les nuits reste armé : « actif » ne dit
    rien du résultat, et c'est la panne la plus discrète du montage."""
    assert alarmes(_etat(
        timer_sauvegarde=UniteEtat("pg-backup.timer", True, True,
                                   "Sat 02:30", "failed")))


def test_des_instantanes_absents_du_bucket_sont_une_alarme():
    """C'est la raison d'être de la copie : locale et distante doivent
    coïncider, et seule la distante survit à la perte du nœud."""
    dits = alarmes(_etat(distants=6, manquants=("20260820-023012",
                                                "20260821-023014")))
    assert any("hors-site" in d or "distant" in d for d in dits)


def test_un_hors_site_non_interroge_nest_pas_un_verdict_vert():
    """Ne pas savoir n'est pas aller bien : si le bucket n'a pas répondu, on le
    dit plutôt que d'afficher une cohérence qu'on n'a pas constatée."""
    dits = alarmes(_etat(distants=None))
    assert dits
    assert "non" in " ".join(dits).lower()


def test_le_rendu_montre_les_deux_cotes_du_montage():
    """Les deux timers ne vivent pas sur la même machine, et c'est la confusion
    la plus facile à faire ici : le rendu dit toujours lequel est où."""
    rendu = render_etat(_etat())
    assert "CT 200" in rendu
    assert "pg-backup.timer" in rendu
    assert "pgbk-offsite.timer" in rendu
    assert "20260821-023014" in rendu


def test_le_rendu_dit_ce_quil_na_pas_pu_constater():
    rendu = render_etat(_etat(distants=None))
    assert "?" in rendu or "non" in rendu.lower()


def test_le_code_de_sortie_suit_les_alarmes():
    from pgtool.etat import code_de_sortie

    assert code_de_sortie(_etat()) == 0
    assert code_de_sortie(_etat(sauvegardes=0, age_heures=None)) == 1


# ─── le constat lui-même ─────────────────────────────────────────────────────


class FauxCtx:
    """Le minimum dont `relever()` a besoin. Les tests purs ci-dessus ne
    l'exécutent jamais : c'est pourtant là que vivent les fautes de frappe."""

    def __init__(self, runner, opts):
        self.runner = runner
        self.opts = opts


def _ctx(sorties):
    from core.runner import FakeRunner, Result
    from pgtool.deploy import Options

    runner = FakeRunner()
    for predicat, sortie in sorties:
        runner.when(predicat, Result(("x",), 0, sortie, ""))
    return FauxCtx(runner, Options(ctid=200, do_offsite=False))


def test_relever_sexecute_de_bout_en_bout():
    from pgtool.etat import relever

    j = " ".join
    ctx = _ctx([
        (lambda a: a[:2] == ("pct", "status"), "status: running\n"),
        (lambda a: "du -sh" in j(a), "8\n20260821-023014\n9\n1.2G\n"),
        (lambda a: "df -m" in j(a), "41200\n"),
        (lambda a: "show" in a, "success\n"),
    ])
    etat = relever(ctx)
    assert etat.ctid == 200
    assert etat.sauvegardes == 8
    assert etat.dernier == "20260821-023014"
    assert etat.age_heures == 9


def test_une_lecture_illisible_laisse_NON_DETERMINE():
    """Une valeur inventée ici deviendrait un verdict vert sur un maillon qu'on
    n'a pas su regarder."""
    from pgtool.etat import relever

    ctx = _ctx([(lambda a: "du -sh" in " ".join(a), "")])
    etat = relever(ctx)
    assert etat.sauvegardes == 0
    assert etat.age_heures is None
    assert etat.libre_mo is None


def test_sans_hors_site_le_bucket_nest_pas_interroge():
    """Et le dire vaut mieux que d'afficher une cohérence non constatée."""
    from pgtool.etat import alarmes, relever

    etat = relever(_ctx([]))
    assert etat.distants is None
    assert any("non constatée" in a for a in alarmes(etat))
