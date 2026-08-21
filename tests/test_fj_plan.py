"""L'ordre du déploiement, vérifié mécaniquement.

L'ordre des étapes est une DONNÉE. Une donnée se vérifie ; une suite d'appels
ne se vérifie que par relecture, et on relit ce qu'on croit avoir écrit.

Ces tests défendent les invariants du parcours, pas le détail de chaque
étape : qu'aucun nom ne soit ambigu, qu'aucun prérequis n'arrive après ce qui
en dépend, et surtout que les DEUX ordres sur lesquels repose ce montage
tiennent — les secrets avant le premier démarrage, la sauvegarde avant la
copie hors-site.
"""

from __future__ import annotations

import pytest

from core.converge import Mode
from core.runner import FakeRunner
from fjtool import plan
from fjtool.deploy import Options, Paths, contexte


@pytest.fixture
def etapes(depot_forgejo):
    ctx = contexte(
        runner=FakeRunner(),
        paths=Paths(src=depot_forgejo),
        opts=Options(ctid=400),
        mode=Mode.STATUS,
    )
    return plan.etapes(ctx)


def _noms(etapes) -> list[str]:
    return [etape.name for etape in etapes]


def _position(etapes, nom: str) -> int:
    return _noms(etapes).index(nom)


# ─── invariants du graphe ────────────────────────────────────────────────────


def test_aucun_nom_en_double(etapes):
    """Deux étapes homonymes rendent toute dépendance ambiguë, et le bilan
    illisible — on ne sait plus laquelle des deux a rendu son verdict."""
    noms = _noms(etapes)
    doublons = sorted({nom for nom in noms if noms.count(nom) > 1})
    assert not doublons, f"noms en double : {doublons}"


def test_toutes_les_dependances_existent(etapes):
    """`traverse` lève sur une dépendance inconnue — mais au premier
    déploiement réel, en root, sur le nœud. Autant l'apprendre ici."""
    connues = set(_noms(etapes))
    orphelines = {
        etape.name: [r for r in etape.requires if r not in connues]
        for etape in etapes
        if any(r not in connues for r in etape.requires)
    }
    assert not orphelines, f"dépendances absentes : {orphelines}"


def test_chaque_prerequis_est_declare_plus_tot(etapes):
    """Un prérequis déclaré APRÈS ce qui en dépend rend l'étape « non
    évaluable » à chaque passage, définitivement : le parcours est linéaire,
    il ne revient jamais en arrière."""
    vus: set[str] = set()
    tardives: dict[str, list[str]] = {}
    for etape in etapes:
        manquants = [r for r in etape.requires if r not in vus]
        if manquants:
            tardives[etape.name] = manquants
        vus.add(etape.name)
    assert not tardives, f"prérequis déclarés trop tard : {tardives}"


# ─── les deux ordres qui portent le montage ──────────────────────────────────


def test_les_secrets_sont_poses_avant_le_premier_demarrage(etapes):
    """L'ordre le plus important du plan.

    Démarrer Forgejo avant que ses quatre secrets ne soient déposés le fait
    les générer lui-même — et tenter de réécrire un `app.ini` qui vient d'un
    montage en LECTURE SEULE. L'écriture échoue, le service continue avec des
    secrets tirés en mémoire, et ils changent à chaque redémarrage : sessions
    invalidées, jetons cassés, aucune erreur ailleurs que trois lignes de
    journal.
    """
    assert _position(etapes, "secrets Forgejo") < _position(etapes, "forgejo (armement)")


def test_la_base_est_posee_avant_le_premier_demarrage(etapes):
    """Sinon le premier démarrage n'est qu'une suite d'échecs de connexion,
    que quelqu'un lira comme une panne."""
    assert _position(etapes, "base forgejo") < _position(etapes, "forgejo (armement)")


def test_la_premiere_sauvegarde_precede_le_hors_site(etapes):
    """Sans elle, la première copie hors-site n'a rien à transférer — et sort
    en « environnement inutilisable » au premier déploiement."""
    assert _position(etapes, "première sauvegarde") < _position(
        etapes, "fjbk-offsite.timer (armement)"
    )


def test_l_outillage_du_noeud_precede_l_installation_binaire(etapes):
    """C'est le NŒUD qui télécharge et qui vérifie : sans `gnupg`, la section
    V n'a pas de quoi valider une signature."""
    assert _position(etapes, "gnupg") < _position(etapes, "clé de publication")


def test_le_montage_est_constate_avant_toute_pose(etapes):
    """La sentinelle d'abord : un `mpN` n'est lu qu'au démarrage, et poser
    depuis un montage absent copie du néant, sans erreur."""
    sentinelle = _position(etapes, "montage /etc/forgejo-git")
    for nom in ("app.ini", "forgejo.service", "pg_hba.conf"):
        assert sentinelle < _position(etapes, nom), f"{nom} posé avant la sentinelle"


def test_les_controles_ferment_le_parcours(etapes):
    """Un contrôle joué au milieu répond sur l'état d'AVANT les poses qui le
    suivent, et rend donc un verdict sur un montage qui n'existe plus."""
    sections = [etape.section for etape in etapes]
    derniers = sections[-8:]
    assert set(derniers) == {"C"}, f"la fin du parcours n'est pas la section C : {derniers}"


# ─── ce que les étapes déclarent ─────────────────────────────────────────────


def test_le_montage_du_depot_est_en_lecture_seule(etapes, depot_forgejo):
    """`ro=1` n'est pas une commodité : ce montage porte `app.ini` et
    `VERSION`. Sans lui, une instance compromise réécrit son propre
    épinglage."""
    ctx = contexte(
        runner=FakeRunner(),
        paths=Paths(src=depot_forgejo),
        opts=Options(ctid=400),
        mode=Mode.STATUS,
    )
    mp1 = next(e for e in etapes if e.name == "mp1")
    voulu = mp1._voulu(ctx)
    assert voulu.readonly, "mp1 doit être monté ro=1"
    assert "ro=1" in voulu.render()
    assert voulu.source.endswith("/ct"), (
        "seul ct/ est monté : le conteneur n'a pas à voir host/ ni doc/"
    )


PVESM = (
    "Name             Type     Status   Total   Used  Available    %\n"
    "data          zfspool   active  100000  20000      80000  20%\n"
    "local             dir   active   50000  10000      40000  20%\n"
)


def _ctx_avec_stockages(depot_forgejo, sortie=PVESM):
    from core.runner import Result

    runner = FakeRunner()
    runner.when("pvesm status", Result(("pvesm", "status"), 0, sortie, ""))
    return contexte(
        runner=runner,
        paths=Paths(src=depot_forgejo),
        opts=Options(ctid=400),
        mode=Mode.STATUS,
    )


def test_le_volume_de_sauvegarde_est_hors_vzdump(etapes, depot_forgejo):
    """`backup=0` tient les dumps hors des `vzdump` du conteneur : sans lui,
    chaque sauvegarde du CT embarquerait toutes les précédentes, et le volume
    doublerait de taille à chaque passage."""
    ctx = _ctx_avec_stockages(depot_forgejo)
    mp2 = next(e for e in etapes if e.name == "mp2")
    resultat = mp2.check(ctx)
    # Sur un CT sans mp2, l'étape propose de le créer : c'est cette proposition
    # qui doit porter backup=0.
    assert resultat.actions, "mp2 absent devrait proposer une création"
    assert "backup=0" in resultat.actions[0].label


def test_un_stockage_inconnu_est_refuse_et_non_devine(etapes, depot_forgejo):
    """`data` existe sur pve-eranikus ; rien ne dit qu'il existe sur pve-ysera.

    Un `pct set` sur un stockage inconnu échouerait au MILIEU du parcours,
    protection déjà levée. Le refus nomme ce qui existe, pour que la
    correction se tape sans aller chercher ailleurs.
    """
    ctx = _ctx_avec_stockages(
        depot_forgejo,
        "Name        Type    Status  Total  Used  Available    %\n"
        "local        dir    active  50000 10000      40000  20%\n",
    )
    mp2 = next(e for e in etapes if e.name == "mp2")
    resultat = mp2.check(ctx)
    assert resultat.state == "error"
    assert not resultat.actions, "rien ne doit être tenté sur un stockage inconnu"
    assert "local" in resultat.detail, "le refus doit nommer ce qui existe"
    assert "--mp2-storage" in resultat.detail, "le refus doit dire quoi taper"


def test_le_moteur_pousse_dans_le_ct_nemporte_jamais_proxmox(etapes, depot_forgejo):
    """Le conteneur n'a pas `pct` et n'a rien à en faire. L'y pousser ferait
    passer les tests du nœud à un import qui n'échouerait que dans le CT."""
    ctx = contexte(
        runner=FakeRunner(),
        paths=Paths(src=depot_forgejo),
        opts=Options(ctid=400),
        mode=Mode.STATUS,
    )
    moteur = next(e for e in etapes if e.name == "moteur (CT)")
    sources = moteur._sources(ctx)
    assert sources, "le moteur doit trouver des modules à pousser"
    assert not [rel for rel in sources if rel.startswith("proxmox/")]
    assert any(rel.startswith("core/") for rel in sources)
    assert any(rel.startswith("fjtool/") for rel in sources)


# ─── les effets ──────────────────────────────────────────────────────────────


def test_tout_effet_declare_a_un_gestionnaire(etapes, depot_forgejo):
    """Un effet déclaré sans gestionnaire est ignoré EN SILENCE par le
    parcours : l'action se joue, le redémarrage qu'elle rendait nécessaire
    n'arrive jamais, et la pose reste sans effet jusqu'au prochain
    redémarrage fortuit."""
    ctx = contexte(
        runner=FakeRunner(),
        paths=Paths(src=depot_forgejo),
        opts=Options(ctid=400),
        mode=Mode.STATUS,
    )
    plan.brancher_effets(ctx)
    branches = set(ctx._handlers)

    declares: set[str] = set()
    for etape in etapes:
        try:
            resultat = etape.check(ctx)
        except Exception:  # noqa: BLE001 - on ne teste pas les étapes ici
            continue
        for action in resultat.actions:
            declares |= set(action.effects)

    orphelins = declares - branches
    assert not orphelins, f"effets sans gestionnaire : {sorted(orphelins)}"
