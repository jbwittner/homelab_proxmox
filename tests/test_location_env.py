"""Ce qui traverse la frontière du conteneur, et ce qui n'en franchit rien.

`pct exec` n'hérite d'AUCUN environnement. Une variable posée sur le nœud est
donc silencieusement perdue — et « silencieusement » est le mot qui compte :
la commande réussit, elle fait simplement autre chose que ce qu'on a demandé.

Le cas qui a motivé ces tests est le garde-fou de l'exercice de bascule.
`PG_BACKUP_DEST=/tmp/pra-backups pg restore pra` tapé depuis le nœud viserait
`/var/backups/postgresql` — le dépôt de PRODUCTION — sans que rien ne le dise.
"""

from __future__ import annotations

import pytest

from core.runner import FakeRunner, Result
from pgtool.location import Delegate


@pytest.fixture
def delegue():
    return Delegate(FakeRunner(), 200)


def test_la_variable_du_depot_traverse_la_frontiere(delegue):
    """Le geste naturel — la poser sur le nœud — doit faire ce qu'il annonce."""
    argv = delegue._argv("restore", ["pra"], env={"PG_BACKUP_DEST": "/tmp/pra"})
    assert "env" in argv
    assert "PG_BACKUP_DEST=/tmp/pra" in argv
    # `env` précède la commande, et les arguments gardent leur ordre.
    assert argv.index("env") < argv.index("restore")
    assert argv[-2:] == ["restore", "pra"]


def test_sans_variable_aucun_env_nest_ajoute(delegue):
    """Ne pas alourdir la ligne de commande ordinaire : `env` sans affectation
    ne sert à rien et brouillerait la lecture d'un `ps`."""
    argv = delegue._argv("list", [], env={})
    assert "env" not in argv


def test_une_variable_vide_ne_traverse_pas(delegue):
    """Une valeur vide vaut « non posée ». La transmettre écraserait le défaut
    du moteur par une chaîne vide, et le dépôt deviendrait le répertoire
    courant — la même faute que `Path("")` qui résout en `.`."""
    argv = delegue._argv("list", [], env={"PG_BACKUP_DEST": ""})
    assert "env" not in argv


def test_seules_les_variables_ATTENDUES_traversent(delegue):
    """Une liste explicite, pas tout l'environnement : passer PATH, LANG ou
    des secrets du nœud dans le conteneur serait une fuite, et les rendre
    visibles dans un `ps` en serait une autre."""
    argv = delegue._argv(
        "list", [], env={"PG_BACKUP_DEST": "/tmp/pra", "AWS_SECRET": "x",
                         "PATH": "/nawak"},
    )
    assert "PG_BACKUP_DEST=/tmp/pra" in argv
    assert not any("AWS_SECRET" in a for a in argv)
    assert not any(a.startswith("PATH=") for a in argv)


def test_le_plan_de_delete_porte_la_meme_variable(delegue):
    """`--plan` résout la référence CONTRE LE DÉPÔT : le faire sur un autre
    dépôt que celui qui sera effacé désignerait un instantané, et en
    supprimerait un autre."""
    delegue.runner.when(lambda a: "--plan" in a,
                        Result(("x",), 0, "20260821-023639\n", ""))
    delegue.plan("delete", ["latest"], env={"PG_BACKUP_DEST": "/tmp/pra"})
    joue = delegue.runner.calls[-1]
    assert "PG_BACKUP_DEST=/tmp/pra" in joue


def test_la_delegation_reelle_porte_la_variable(monkeypatch, delegue):
    """Le chemin qui compte vraiment : celui qui remplace le processus."""
    vu = []
    monkeypatch.setattr(delegue.runner, "exec_replace",
                        lambda *argv: vu.append(argv))
    delegue.hand_over("restore", ["pra"], yes=True,
                      env={"PG_BACKUP_DEST": "/tmp/pra"})
    assert "PG_BACKUP_DEST=/tmp/pra" in vu[0]
    assert "--yes" in vu[0]


def test_la_facade_transmet_reellement_lenvironnement(monkeypatch):
    """Le mecanisme peut exister sans jamais etre appele : ce test suit le
    chemin complet, de la ligne de commande jusqu'a l'argv joue."""
    import pgtool.cli as cli
    from core.runner import FakeRunner
    from pgtool.location import Delegate

    vu = []
    monkeypatch.setattr(Delegate, "preflight", lambda self: None)
    monkeypatch.setattr(Delegate, "hand_over",
                        lambda self, c, a, *, yes, env=None: vu.append(env))
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setenv("PG_BACKUP_DEST", "/tmp/pra")
    monkeypatch.setenv("PG_CTID", "200")

    args = cli.build_parser().parse_args(["restore", "pra", "--yes"])
    cli._acheminer(args, FakeRunner())
    assert vu and vu[0].get("PG_BACKUP_DEST") == "/tmp/pra"
