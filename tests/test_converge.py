"""Le moteur de convergence : trois modes, un seul parcours.

Ce que ces tests défendent tient en une phrase : **le plan est produit par
`check()`, jamais par `apply()`**. C'est ce qui fait qu'il n'y a plus de cas de
simulation à oublier — il n'y en a plus à écrire.

Le bash répétait quarante fois « constater / afficher en dry-run / appliquer »,
et sept de ses mutations ne passaient déjà pas par le garde `run()`.
"""

from __future__ import annotations

import pytest

from core.converge import (
    Action,
    Context,
    Mode,
    Outcome,
    Step,
    render_summary,
    traverse,
)


# ─── de quoi fabriquer des étapes ────────────────────────────────────────────


class Etape:
    """Une étape de test : on lui dit quoi constater, elle note ce qu'on fait."""

    def __init__(self, nom, *, section="A", state="ok", actions=(),
                 requires=(), skip=None):
        self.name = nom
        self.section = section
        self.requires = requires
        self._state = state
        self._actions = tuple(actions)
        self._skip = skip
        self.checks = 0

    def skip_if(self, ctx):
        return self._skip

    def check(self, ctx):
        self.checks += 1
        return Outcome(self._state, f"détail de {self.name}", self._actions)


def _action(nom, journal, **kw):
    return Action(nom, lambda ctx: journal.append(nom), **kw)


def _ctx(mode):
    return Context(mode=mode)


# ─── le plan vient de check(), et de lui seul ────────────────────────────────


def test_un_etat_conforme_na_pas_daction():
    """« Zéro modification sur un état conforme » n'est plus une discipline,
    c'est `actions == ()`."""
    rapports = traverse([Etape("a")], _ctx(Mode.APPLY))
    assert rapports[0].state == "ok"
    assert rapports[0].applied == ()


def test_apply_execute_les_actions_du_plan():
    journal = []
    etape = Etape("a", state="drift", actions=[_action("poser", journal)])
    rapports = traverse([etape], _ctx(Mode.APPLY))
    assert journal == ["poser"]
    assert rapports[0].applied == ("poser",)


def test_dry_run_annonce_sans_rien_faire(capsys):
    journal = []
    etape = Etape("a", state="drift", actions=[_action("poser", journal)])
    traverse([etape], _ctx(Mode.DRY_RUN))
    assert journal == [], "la simulation ne doit rien exécuter"
    assert "[dry-run] poser" in capsys.readouterr().out


def test_status_najoute_rien_a_la_simulation(capsys):
    """--status et --dry-run sont le MÊME parcours : inventer une troisième
    sémantique donnerait un troisième jeu de cas à oublier."""
    journal = []
    etape = Etape("a", state="drift", actions=[_action("poser", journal)])
    traverse([etape], _ctx(Mode.STATUS))
    assert journal == []
    assert "[dry-run]" not in capsys.readouterr().out, "status ne détaille pas"


def test_la_description_du_plan_nexiste_quune_fois():
    """L'action porte son propre libellé : il n'y a pas une phrase pour la
    simulation et un code pour l'exécution, donc rien à désynchroniser."""
    journal = []
    etape = Etape("a", state="drift", actions=[_action("pct set 200 --mp1 …", journal)])
    plan = etape.check(_ctx(Mode.STATUS))
    assert [a.label for a in plan.actions] == ["pct set 200 --mp1 …"]


# ─── check() n'est jamais sauté ──────────────────────────────────────────────


def test_check_est_appele_dans_les_trois_modes():
    for mode in (Mode.APPLY, Mode.DRY_RUN, Mode.STATUS):
        etape = Etape("a")
        traverse([etape], _ctx(mode))
        assert etape.checks == 1, mode


def test_une_etape_sautee_nest_pas_constatee():
    etape = Etape("a", skip="--no-offsite")
    rapports = traverse([etape], _ctx(Mode.APPLY))
    assert etape.checks == 0
    assert rapports[0].state == "skip"
    assert "--no-offsite" in rapports[0].detail


# ─── effets coalescés ────────────────────────────────────────────────────────


def test_trois_poses_ne_font_quun_seul_rechargement():
    """Le bash portait un drapeau `copied` mis à la main par chaque copie. Ici
    l'action DÉCLARE son effet, et le parcours le coalesce."""
    journal = []
    etapes = [
        Etape(f"unite{i}", state="drift",
              actions=[_action(f"install {i}", journal,
                               effects=frozenset({"ct.daemon-reload"}))])
        for i in range(3)
    ]
    ctx = _ctx(Mode.APPLY)
    ctx.on_effect("ct.daemon-reload", lambda c: journal.append("daemon-reload"))
    traverse(etapes, ctx)
    assert journal.count("daemon-reload") == 1


def test_un_effet_non_declenche_ne_seffectue_pas():
    journal = []
    ctx = _ctx(Mode.APPLY)
    ctx.on_effect("ct.reboot", lambda c: journal.append("reboot"))
    traverse([Etape("a")], ctx)
    assert journal == []


def test_les_effets_sont_vides_apres_toutes_les_etapes():
    """« Redémarrer APRÈS tous les pct set » ne s'exprime pas en ordre de
    déclaration mais en effet coalescé, vidé à la fin."""
    journal = []
    etapes = [
        Etape("mp1", state="drift",
              actions=[_action("pct set --mp1", journal,
                               effects=frozenset({"ct.reboot"}))]),
        Etape("mp2", state="drift",
              actions=[_action("pct set --mp2", journal,
                               effects=frozenset({"ct.reboot"}))]),
    ]
    ctx = _ctx(Mode.APPLY)
    ctx.on_effect("ct.reboot", lambda c: journal.append("reboot"))
    traverse(etapes, ctx)
    assert journal == ["pct set --mp1", "pct set --mp2", "reboot"]


def test_aucun_effet_en_simulation():
    journal = []
    etape = Etape("a", state="drift",
                  actions=[_action("poser", journal,
                                   effects=frozenset({"ct.reboot"}))])
    ctx = _ctx(Mode.DRY_RUN)
    ctx.on_effect("ct.reboot", lambda c: journal.append("reboot"))
    traverse([etape], ctx)
    assert journal == []


# ─── ce qui n'est pas évaluable ──────────────────────────────────────────────


def test_une_dependance_non_posee_rend_letape_suivante_inevaluable():
    """En simulation, rien n'a été appliqué : une étape dont le prérequis
    reste à poser ne peut RIEN conclure. Le bash disait « pose non evaluable »
    au cas par cas ; c'est une règle du parcours désormais."""
    montage = Etape("mp1", state="drift", actions=[Action("poser", lambda c: None)])
    pose = Etape("fichiers", requires=("mp1",))
    rapports = traverse([montage, pose], _ctx(Mode.DRY_RUN))
    assert rapports[1].state == "unknown"
    assert pose.checks == 0, "inutile de constater ce qu'on ne peut pas voir"


def test_en_apply_la_dependance_est_posee_donc_letape_est_evaluable():
    """Le parcours est ENTRELACÉ : constater, appliquer, puis constater la
    suivante. Tout batcher ferait observer un état d'avant."""
    montage = Etape("mp1", state="drift", actions=[Action("poser", lambda c: None)])
    pose = Etape("fichiers", requires=("mp1",))
    rapports = traverse([montage, pose], _ctx(Mode.APPLY))
    assert rapports[1].state == "ok"
    assert pose.checks == 1


def test_une_dependance_conforme_ne_bloque_personne():
    montage = Etape("mp1")           # ok, aucune action
    pose = Etape("fichiers", requires=("mp1",))
    rapports = traverse([montage, pose], _ctx(Mode.DRY_RUN))
    assert rapports[1].state == "ok"


def test_une_dependance_inconnue_est_une_erreur_de_programmation():
    """Une faute de frappe dans `requires` ne doit pas passer inaperçue en
    laissant l'étape s'exécuter comme si de rien n'était."""
    with pytest.raises(KeyError, match="absente"):
        traverse([Etape("a", requires=("nexiste-pas",))], _ctx(Mode.APPLY))


# ─── secrets ─────────────────────────────────────────────────────────────────


def test_une_action_a_secret_nest_pas_jouee_sans_demande():
    """Rejouer un déploiement de routine ne doit pas faire apparaître un mot de
    passe dans un terminal ni en créer un dont personne n'attend la rotation."""
    journal = []
    etape = Etape("role", state="absent",
                  actions=[_action("CREATE ROLE", journal, generates_secret=True)])
    rapports = traverse([etape], _ctx(Mode.APPLY))
    assert journal == []
    assert rapports[0].state == "blocked"


def test_une_action_a_secret_est_jouee_si_demandee():
    journal = []
    etape = Etape("role", state="absent",
                  actions=[_action("CREATE ROLE", journal, generates_secret=True)])
    ctx = _ctx(Mode.APPLY)
    ctx.allow_secrets = True
    traverse([etape], ctx)
    assert journal == ["CREATE ROLE"]


# ─── le résumé ───────────────────────────────────────────────────────────────


def test_le_resume_reprend_les_trois_verdicts_du_bash():
    """OK / POSE / KO, une ligne par élément, dans l'ordre d'exécution."""
    rapports = traverse(
        [
            Etape("nesting"),
            Etape("mp1", state="drift", actions=[Action("poser", lambda c: None)]),
            Etape("rclone", state="error"),
        ],
        _ctx(Mode.STATUS),
    )
    lignes = render_summary(rapports).splitlines()
    assert lignes[0].split() == ["OK", "nesting"]
    assert lignes[1].split() == ["POSE", "mp1"]
    assert lignes[2].split() == ["KO", "rclone"]


def test_le_resume_garde_lordre_dexecution():
    """L'ordre des lignes est un contrat : on lit le résumé de haut en bas
    comme on a joué les étapes."""
    rapports = traverse([Etape("z"), Etape("a"), Etape("m")], _ctx(Mode.STATUS))
    assert [r.step for r in rapports] == ["z", "a", "m"]


def test_une_etape_inevaluable_compte_comme_KO():
    """Le bash notait KO sur « pose non evaluable » : ne pas savoir n'est pas
    la même chose que d'aller bien."""
    montage = Etape("mp1", state="drift", actions=[Action("poser", lambda c: None)])
    pose = Etape("fichiers", requires=("mp1",))
    rapports = traverse([montage, pose], _ctx(Mode.DRY_RUN))
    assert render_summary(rapports).splitlines()[1].split()[0] == "KO"


def test_une_etape_sautee_ne_figure_pas_au_resume():
    rapports = traverse([Etape("a"), Etape("b", skip="--no-offsite")],
                        _ctx(Mode.STATUS))
    assert len(render_summary(rapports).splitlines()) == 1


# ─── filets du parcours ──────────────────────────────────────────────────────


class EtapeQuiLeve:
    name = "casse"
    section = "F"
    requires: tuple = ()

    def skip_if(self, ctx):
        return None

    def check(self, ctx):
        raise RuntimeError("pvesm a disparu")


def test_une_etape_qui_leve_ne_abat_pas_le_parcours():
    """En --status on perdrait le reste du bilan, et c'est justement le moment
    où l'on veut tout voir. Le défaut est rapporté sur SA ligne."""
    rapports = traverse([EtapeQuiLeve(), Etape("suivante")], _ctx(Mode.STATUS))
    assert rapports[0].state == "error"
    assert "pvesm a disparu" in rapports[0].detail
    assert rapports[1].state == "ok", "la suite est quand même constatée"


def test_un_prerequis_en_echec_rend_le_dependant_inevaluable_meme_en_apply():
    """Un prérequis qui a ÉCHOUÉ n'a pas eu lieu, quel que soit le mode. Le
    prétendre serait pire que de l'avouer."""
    rapports = traverse(
        [Etape("socle", state="error"), Etape("dessus", requires=("socle",))],
        _ctx(Mode.APPLY),
    )
    assert rapports[1].state == "unknown"


def test_un_prerequis_bloque_rend_le_dependant_inevaluable():
    """Une action à secret non demandée bloque son étape ; ce qui en dépend
    n'est pas plus évaluable."""
    secrete = Etape("role", state="absent",
                    actions=[Action("CREATE ROLE", lambda c: None,
                                    generates_secret=True)])
    rapports = traverse(
        [secrete, Etape("locataire", requires=("role",))], _ctx(Mode.APPLY)
    )
    assert rapports[0].state == "blocked"
    assert rapports[1].state == "unknown"
