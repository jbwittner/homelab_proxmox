"""Le CLI de bout en bout, sans jamais rien exécuter.

`pct` est bouchonné, `os.execvp` intercepté, la saisie clavier injectée. On
vérifie ce qui part vers le conteneur, et ce qui ne part pas.
"""

from __future__ import annotations

import builtins

import pytest

import core.runner as runner_mod
import pgtool.location as location
from core.runner import FakeRunner, Result
from pgtool.cli import DELEGUEES, _positionnels, build_parser, main
from pgtool.location import CT_PGBK, Where


@pytest.fixture
def noeud(monkeypatch):
    """Un nœud Proxmox complaisant : CT 200 présent, démarré, moteur posé."""
    faux = FakeRunner()
    faux.when(lambda a: a[:2] == ("pct", "status"),
              Result(("pct",), 0, "status: running\n", ""))

    monkeypatch.setattr(runner_mod, "Runner", lambda *a, **k: faux)
    monkeypatch.setattr(location, "detect", lambda _r: Where.HOST)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr(location, "read_conf", lambda *a, **k: {"PG_CTID": "200"})

    execs: list[list[str]] = []
    monkeypatch.setattr("os.execvp", lambda f, a: execs.append(a))
    faux.execs = execs
    return faux


def _lance(argv: list[str]) -> int:
    return main(argv)


# ─── reconstitution des arguments ────────────────────────────────────────────


def test_toutes_les_commandes_du_moteur_sont_exposees():
    """Le bash en portait six ; il n'en manque aucune."""
    assert set(DELEGUEES) == {"backup", "list", "show", "restore", "verify", "delete"}


def test_un_positionnel_facultatif_absent_ne_laisse_pas_de_trou():
    """Le moteur lit $1 et $2, pas des options nommées : un trou décalerait
    tout ce qui suit."""
    args = build_parser().parse_args(["restore", "forge"])
    assert _positionnels(args, "restore") == ["forge"]


def test_un_positionnel_facultatif_fourni_est_transmis():
    args = build_parser().parse_args(["restore", "forge", "20260819"])
    assert _positionnels(args, "restore") == ["forge", "20260819"]


def test_show_sans_argument_ne_transmet_rien():
    """Le moteur applique alors son défaut, « latest »."""
    args = build_parser().parse_args(["show"])
    assert _positionnels(args, "show") == []


# ─── acheminement ────────────────────────────────────────────────────────────


def test_list_est_achemine_tel_quel(noeud):
    _lance(["list"])
    assert noeud.execs == [["pct", "exec", "200", "--", CT_PGBK, "list"]]


def test_le_ctid_du_drapeau_est_utilise(noeud):
    _lance(["--ctid", "299", "list"])
    assert noeud.execs[0][2] == "299"


def test_backup_ne_demande_aucune_confirmation(noeud, monkeypatch):
    """Sauvegarder n'écrase rien."""
    monkeypatch.setattr(builtins, "input", lambda _: pytest.fail("question inattendue"))
    _lance(["backup"])
    assert noeud.execs[0][-1] == "backup"


# ─── restore : la confirmation est posée sur le nœud ─────────────────────────


def test_restore_demande_le_nom_de_la_base(noeud, monkeypatch):
    """`pct exec` n'alloue pas de TTY : une question posée depuis le conteneur
    ne verrait jamais la saisie."""
    vues = []
    monkeypatch.setattr(builtins, "input", lambda q: (vues.append(q), "forge")[1])
    _lance(["restore", "forge"])
    assert "ÉCRASE la base forge du CT 200" in vues[0]
    assert noeud.execs[0][-1] == "--yes"


def test_restore_refuse_sur_une_reponse_approchante(noeud, monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", lambda _: "forg")
    assert _lance(["restore", "forge"]) == 1
    assert noeud.execs == [], "rien ne doit partir vers le conteneur"
    assert "annulé" in capsys.readouterr().err


def test_restore_avec_yes_ne_demande_rien(noeud, monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda _: pytest.fail("question inattendue"))
    _lance(["restore", "forge", "--yes"])
    assert noeud.execs[0][-1] == "--yes"


# ─── delete : la question porte sur ce qui sera supprimé ─────────────────────


def test_delete_confirme_sur_le_nom_RESOLU(noeud, monkeypatch):
    """« 20260819 » désigne la plus récente de ce jour-là. La question doit
    porter sur ce qui sera réellement supprimé, pas sur ce qui a été tapé."""
    noeud.when("--plan", Result(("pct",), 0, "20260819-233627\n", ""))
    vues = []
    monkeypatch.setattr(
        builtins, "input", lambda q: (vues.append(q), "20260819-233627")[1]
    )
    _lance(["delete", "20260819"])
    assert "SUPPRIME l'instantané 20260819-233627 du CT 200" in vues[0]
    assert noeud.execs[0][-2:] == ["20260819", "--yes"]


def test_delete_plan_sarrete_et_neffacerien(noeud, capsys, monkeypatch):
    """Le bash enchaînait sur la suppression : « --plan » n'y était honnête
    que dans le conteneur. Ici il s'arrête, ce que son nom promet."""
    noeud.when("--plan", Result(("pct",), 0, "20260819-233627\n", ""))
    monkeypatch.setattr(builtins, "input", lambda _: pytest.fail("question inattendue"))
    assert _lance(["delete", "20260819", "--plan"]) == 0
    assert noeud.execs == [], "aucune délégation, donc aucune suppression"
    assert capsys.readouterr().out.strip() == "20260819-233627"


def test_delete_refuse_si_le_moteur_refuse(noeud, monkeypatch, capsys):
    """Le dernier instantané est protégé : la garde est côté moteur, et son
    refus doit arrêter la façade avant toute question."""
    noeud.when("--plan", Result(("pct",), 1, "", "est le dernier instantané — protégé"))
    monkeypatch.setattr(builtins, "input", lambda _: pytest.fail("question inattendue"))
    assert _lance(["delete", "20260820"]) == 1
    assert noeud.execs == []
    assert "dernier instantané" in capsys.readouterr().err


def test_delete_sans_cible_ne_demande_rien(noeud, monkeypatch, capsys):
    noeud.when("--plan", Result(("pct",), 0, "\n", ""))
    monkeypatch.setattr(builtins, "input", lambda _: pytest.fail("question inattendue"))
    assert _lance(["delete", "20260819"]) == 1
    assert "rien à supprimer" in capsys.readouterr().err


def test_delete_annule_ne_delegue_pas(noeud, monkeypatch, capsys):
    noeud.when("--plan", Result(("pct",), 0, "20260819-233627\n", ""))
    monkeypatch.setattr(builtins, "input", lambda _: "20260819")
    assert _lance(["delete", "20260819"]) == 1
    assert noeud.execs == []
    assert "annulé" in capsys.readouterr().err


# ─── refus d'emplacement ─────────────────────────────────────────────────────


def test_dans_le_conteneur_cest_le_moteur_qui_repond(monkeypatch, capsys, tmp_path):
    """Un seul fichier, deux rôles : sur le nœud il achemine, dans le CT il
    travaille. C'est la présence de `pct` qui tranche, et rien d'autre."""
    dest = tmp_path / "postgresql" / "20260820-093240"
    dest.mkdir(parents=True)
    for f in ("globals.sql", "forge.dump", "MANIFEST"):
        (dest / f).write_text("x")

    monkeypatch.setattr(location, "detect", lambda _r: Where.CONTAINER)
    monkeypatch.setattr(runner_mod, "Runner", lambda *a, **k: FakeRunner())
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setenv("PG_BACKUP_DEST", str(tmp_path / "postgresql"))

    assert main(["list"]) == 0
    sortie = capsys.readouterr().out
    assert "20260820-093240" in sortie
    assert "← latest" in sortie


def test_dans_le_conteneur_pct_nest_jamais_appele(monkeypatch, tmp_path):
    """Le moteur ne connaît pas Proxmox. S'il appelait `pct`, il échouerait
    dans le seul endroit où il est censé tourner."""
    (tmp_path / "postgresql").mkdir()
    faux = FakeRunner()
    monkeypatch.setattr(location, "detect", lambda _r: Where.CONTAINER)
    monkeypatch.setattr(runner_mod, "Runner", lambda *a, **k: faux)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setenv("PG_BACKUP_DEST", str(tmp_path / "postgresql"))

    main(["list"])
    assert not any(argv and argv[0] == "pct" for argv in faux.calls)


def test_dans_le_conteneur_sans_root_on_refuse(monkeypatch, capsys):
    monkeypatch.setattr(location, "detect", lambda _r: Where.CONTAINER)
    monkeypatch.setattr(runner_mod, "Runner", lambda *a, **k: FakeRunner())
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    assert main(["list"]) == 1
    assert "pct enter" in capsys.readouterr().err


def test_sans_root_on_refuse_avant_de_toucher_a_quoi_que_ce_soit(
    monkeypatch, capsys
):
    monkeypatch.setattr(location, "detect", lambda _r: Where.HOST)
    monkeypatch.setattr(runner_mod, "Runner", lambda *a, **k: FakeRunner())
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    assert main(["list"]) == 1
    assert "root" in capsys.readouterr().err


def test_un_refus_ne_produit_pas_de_trace(noeud, monkeypatch, capsys):
    """Le message a déjà tout dit ; une pile Python le noierait."""
    monkeypatch.setattr(location, "read_conf", lambda *a, **k: {})
    monkeypatch.setattr("os.environ", {})
    assert main(["list"]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "aucun conteneur cible" in err


def test_un_incident_imprevu_sort_en_un(noeud, monkeypatch, capsys):
    """Laisser échapper un code arbitraire, comme le `exit $rc` du bash,
    casserait le contrat que systemd et les habitudes supposent."""
    def boum(*a, **k):
        raise ValueError("inattendu")

    monkeypatch.setattr(location, "resolve_ctid", boum)
    assert main(["list"]) == 1
    assert "échec inattendu" in capsys.readouterr().err


# ─── codes de retour de l'analyse d'arguments ────────────────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        ["offsite", "--foo"],       # option inconnue
        ["inconnu"],                # sous-commande inconnue
        [],                         # aucune commande
        ["restore"],                # positionnel obligatoire manquant
        ["--ctid"],                 # valeur manquante
    ],
    ids=["option", "commande", "vide", "positionnel", "valeur"],
)
def test_un_usage_fautif_sort_en_un(argv):
    """argparse sort en 2 de lui-même. Or 2 veut dire « au moins un transfert a
    échoué » : une faute de frappe serait consignée par systemd comme une panne
    de transfert, et se lirait comme telle trois semaines plus tard."""
    with pytest.raises(SystemExit) as sortie:
        main(argv)
    assert sortie.value.code == 1


def test_laide_nest_pas_une_erreur():
    with pytest.raises(SystemExit) as sortie:
        main(["--help"])
    assert sortie.value.code == 0


def test_laide_dune_sous_commande_nest_pas_une_erreur():
    """Les sous-analyseurs héritent de la classe du parent : sans cela ils
    garderaient le code 2 d'argparse."""
    with pytest.raises(SystemExit) as sortie:
        main(["offsite", "--help"])
    assert sortie.value.code == 0


# ─── parité d'affichage avec le moteur bash ──────────────────────────────────


def test_le_tableau_est_une_donnee_le_bilan_est_un_message(
    monkeypatch, capsys, tmp_path
):
    """Le bash horodate la seule ligne de bilan, pas les lignes du tableau —
    et c'est exactement la distinction posée dans log.py : une donnée se
    recopie telle quelle, un message porte l'heure et son niveau.

    Écart constaté à la comparaison des deux moteurs le 21 août 2026 :

        bash   : 10:17:14 [INFO ] 8 sauvegarde(s), 34K — 51200 Mo libres
        python : 8 sauvegarde(s), 34K — 51200 Mo libres
    """
    import re

    dest = tmp_path / "postgresql" / "20260820-093240"
    dest.mkdir(parents=True)
    for f in ("globals.sql", "forge.dump", "MANIFEST"):
        (dest / f).write_text("x")

    monkeypatch.setattr(location, "detect", lambda _r: Where.CONTAINER)
    monkeypatch.setattr(runner_mod, "Runner", lambda *a, **k: FakeRunner())
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setenv("PG_BACKUP_DEST", str(tmp_path / "postgresql"))

    main(["list"])
    lignes = [l for l in capsys.readouterr().out.splitlines() if l]

    horodate = re.compile(r"^\d{2}:\d{2}:\d{2} \[INFO \] ")
    bilan = [l for l in lignes if "sauvegarde(s)" in l]
    assert bilan and horodate.match(bilan[0]), "le bilan est un message"

    tableau = [l for l in lignes if "20260820-093240" in l or "INSTANTANÉ" in l]
    assert tableau, "le tableau doit être là"
    for ligne in tableau:
        assert not horodate.match(ligne), "une donnée ne s'horodate pas"
