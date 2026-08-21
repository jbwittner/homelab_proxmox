"""L'ordre du déploiement, vérifié mécaniquement.

L'ordre des étapes est une DONNÉE. Une donnée se vérifie ; une suite d'appels
ne se vérifie que par relecture, et on relit ce qu'on croit avoir écrit.

Ces tests défendent les invariants du parcours, pas le détail de chaque
étape : qu'aucun nom ne soit ambigu, qu'aucun prérequis n'arrive après ce qui
en dépend, et surtout que les ordres sur lesquels repose ce montage tiennent —
le mot de passe avant la configuration qui le porte, les secrets et la base
avant le premier démarrage.

Un test y veille aussi à ce qui NE DOIT PLUS être là : depuis que la base est
un locataire du CT 200, ce déploiement ne sauvegarde rien et ne copie rien
hors-site. Une étape qui réapparaîtrait donnerait deux filets pour un même
objet, dont un que personne ne surveille.
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


def test_la_connexion_a_la_base_precede_le_premier_demarrage(etapes):
    """Sinon le premier démarrage n'est qu'une suite d'échecs
    d'authentification, que quelqu'un lira comme une panne de Forgejo alors
    que c'est une ligne manquante dans le `pg_hba.conf` du CT 200."""
    assert _position(etapes, "connexion à la base (CT 200)") < _position(
        etapes, "forgejo (armement)"
    )


def test_le_mot_de_passe_precede_la_configuration_qui_le_porte(etapes):
    """`app.ini` est RENDU avec le mot de passe substitué. Le rendre avant que
    le secret soit là produirait une configuration portant le marqueur
    `@@DB_PASSWORD@@` en guise de mot de passe — et un échec
    d'authentification qui ne dirait pas pourquoi."""
    assert _position(etapes, "mot de passe de la base") < _position(
        etapes, "app.ini"
    )


def test_aucune_etape_ne_sauvegarde_ni_ne_copie_hors_site(etapes):
    """La base est un locataire du CT 200 : `pg` la sauvegarde et l'emporte
    hors-site. Les dépôts partent par `vzdump`. Une étape de sauvegarde ici
    donnerait deux filets pour un même objet, dont un que personne ne
    surveille.

    **La section H est exclue, et ce n'est pas une échappatoire.** Elle nomme
    précisément ce qui ne doit PAS être là — « retrait de fjbk-offsite.timer »
    est l'inverse d'une étape de sauvegarde. Sans cette exclusion, ce test
    interdirait de nettoyer ce qu'il existe justement pour interdire.
    """
    noms = " ".join(e.name for e in etapes if e.section != "H").lower()
    for interdit in ("sauvegarde", "hors-site", "offsite", "backup"):
        assert interdit not in noms, f"« {interdit} » n'a plus lieu d'être ici"


def test_le_hors_site_perime_est_desarme_avant_dtre_retire(etapes):
    """Retirer le fichier d'une unité encore armée laisse un lien pendant dans
    `timers.target.wants`, et systemd s'en plaint à chaque `daemon-reload`
    sans que personne fasse le rapprochement.

    Et le TIMER passe avant le SERVICE : désarmer le second en premier
    laisserait un timer pointant sur une unité disparue.
    """
    assert _position(etapes, "retrait de fjbk-offsite.timer") < _position(
        etapes, "retrait de fjbk-offsite.service"
    )


def test_l_outillage_du_noeud_precede_l_installation_binaire(etapes):
    """C'est le NŒUD qui télécharge et qui vérifie : sans `gnupg`, la section
    V n'a pas de quoi valider une signature."""
    assert _position(etapes, "gnupg") < _position(etapes, "clé de publication")


def test_le_montage_est_constate_avant_toute_pose(etapes):
    """La sentinelle d'abord : un `mpN` n'est lu qu'au démarrage, et poser
    depuis un montage absent copie du néant, sans erreur."""
    sentinelle = _position(etapes, "montage /etc/forgejo-git")
    for nom in ("app.ini", "forgejo.service", "/etc/forgejo"):
        assert sentinelle < _position(etapes, nom), f"{nom} posé avant la sentinelle"


def test_les_controles_ferment_le_parcours(etapes):
    """Un contrôle joué au milieu répond sur l'état d'AVANT les poses qui le
    suivent, et rend donc un verdict sur un montage qui n'existe plus."""
    sections = [etape.section for etape in etapes]
    derniers = sections[-4:]
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
