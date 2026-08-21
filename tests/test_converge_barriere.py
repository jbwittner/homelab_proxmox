"""L'étape-barrière : où les effets coalescés sont vidés.

« Redémarrer APRÈS tous les `pct set` » ne s'exprime pas par un ordre de
déclaration mais par un effet coalescé — et il faut bien un endroit où le
vider. Cet endroit ne peut pas être la fin du parcours : la section suivante
observerait alors un conteneur d'avant son redémarrage, c'est-à-dire un
montage encore vide.
"""

from __future__ import annotations

from core.converge import Action, Barrier, Context, Mode, Outcome, traverse


class Pose:
    """Une étape qui pose, et déclare qu'un redémarrage suivra."""

    name, section, requires = "pose", "A", ()

    def __init__(self, journal):
        self.journal = journal

    def skip_if(self, ctx):
        return None

    def check(self, ctx):
        return Outcome("absent", "", (
            Action("pct set", lambda c: self.journal.append("set"),
                   effects=frozenset({"ct.reboot"})),
        ))


class Regarde:
    """Une étape de la section suivante, qui constate."""

    name, section, requires = "regarde", "B", ()

    def __init__(self, journal):
        self.journal = journal

    def skip_if(self, ctx):
        return None

    def check(self, ctx):
        self.journal.append("check B")
        return Outcome("ok")


def test_la_barriere_vide_les_effets_avant_la_suite():
    journal = []
    ctx = Context(mode=Mode.APPLY)
    ctx.on_effect("ct.reboot", lambda c: journal.append("reboot"))
    traverse([Pose(journal), Barrier("fin de section A", "A"),
              Regarde(journal)], ctx)
    assert journal == ["set", "reboot", "check B"]


def test_une_barriere_sans_effet_en_attente_est_verte():
    rapports = traverse([Barrier("fin de section A", "A")],
                        Context(mode=Mode.APPLY))
    assert rapports[0].state == "ok"


def test_en_simulation_la_barriere_ANNONCE_le_redemarrage(capsys):
    """C'est le mode où « il faudra redémarrer le CT » compte le plus. Une
    barrière qui attendrait l'exécution n'aurait rien à annoncer là où on veut
    justement le savoir d'avance."""
    journal = []
    ctx = Context(mode=Mode.DRY_RUN)
    ctx.on_effect("ct.reboot", lambda c: journal.append("reboot"))
    traverse([Pose(journal), Barrier("fin de section A", "A")], ctx)
    assert journal == [], "en simulation, rien n'est joué — ni action, ni effet"
    capture = capsys.readouterr()
    assert "ct.reboot" in capture.out + capture.err


def test_un_effet_nest_annonce_quune_fois_en_simulation(capsys):
    """Annoncé par la barrière, il ne doit pas ressortir dans le bilan final :
    deux mentions se liraient comme deux redémarrages."""
    journal = []
    ctx = Context(mode=Mode.DRY_RUN)
    ctx.on_effect("ct.reboot", lambda c: journal.append("reboot"))
    traverse([Pose(journal), Barrier("fin de section A", "A")], ctx)
    capture = capsys.readouterr()
    assert (capture.out + capture.err).count("ct.reboot") == 1


def test_en_simulation_un_effet_sans_barriere_est_annonce_a_la_fin(capsys):
    """Sans barrière, l'information ne doit pas se perdre : le parcours dit à
    la fin ce qu'il aurait fallu faire."""
    journal = []
    ctx = Context(mode=Mode.DRY_RUN)
    ctx.on_effect("ct.reboot", lambda c: journal.append("reboot"))
    traverse([Pose(journal)], ctx)
    assert journal == []
    capture = capsys.readouterr()
    assert "ct.reboot" in capture.out + capture.err
