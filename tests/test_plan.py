"""L'ordre du déploiement, et ce qu'il garantit.

Le bash portait son ordre dans la suite des appels de `main()`. Ici il est une
DONNÉE, donc relisible — et vérifiable. Ces tests portent sur des propriétés de
la liste, pas sur ce que chaque étape fait : c'est la seule façon d'attraper la
classe de défauts qui coûte le plus cher ici, celle où une étape regarde avant
que ce qu'elle regarde n'ait été posé.
"""

from __future__ import annotations

import pytest

from core.converge import Barrier, Mode
from core.runner import FakeRunner
from pgtool.deploy import Options, Paths, contexte
from pgtool.plan import etapes


@pytest.fixture
def ctx(tmp_path):
    service = tmp_path / "depot" / "pve-eranikus" / "pgsql"
    (service / "ct").mkdir(parents=True)
    (service / "host").mkdir()
    return contexte(
        runner=FakeRunner(),
        paths=Paths(src=service),
        opts=Options(ctid=200),
        mode=Mode.STATUS,
    )


def test_chaque_prerequis_est_satisfait_PLUS_TOT(ctx):
    """`traverse` refuse un prérequis inconnu, mais pas un prérequis déclaré
    APRÈS. Or un prérequis qui vient après n'est pas encore constaté : l'étape
    conclurait dans le vide au lieu de se dire non évaluable — exactement le
    piège du montage, qu'un ordre inversé ferait réapparaître en silence."""
    vus: set[str] = set()
    for etape in etapes(ctx):
        for prerequis in etape.requires:
            assert prerequis in vus, (
                f"« {etape.name} » exige « {prerequis} », déclaré plus tard"
            )
        vus.add(etape.name)


def test_aucun_nom_detape_nest_en_double(ctx):
    """Deux étapes du même nom rendraient une dépendance ambiguë, et le bilan
    illisible."""
    noms = [e.name for e in etapes(ctx)]
    assert len(noms) == len(set(noms))


def test_le_montage_est_constate_avant_toute_pose_dans_le_CT(ctx):
    """Un mpN n'est lu qu'au démarrage : poser depuis un montage qu'on n'a pas
    constaté copierait du néant, sans le moindre message d'erreur."""
    noms = [e.name for e in etapes(ctx)]
    sentinelle = noms.index("montage /etc/pgsql-git")
    assert noms.index("pg-backup.sh") > sentinelle
    assert noms.index("pg_hba.conf") > sentinelle


def test_une_barriere_separe_les_prerequis_du_CT_de_la_pose(ctx):
    """C'est elle qui redémarre le conteneur : sans elle, la section B
    regarderait un montage que le CT n'a pas encore relu."""
    liste = etapes(ctx)
    positions = [i for i, e in enumerate(liste) if isinstance(e, Barrier)]
    assert positions, "aucune barrière : les effets ne seraient vidés qu'à la fin"

    noms = [e.name for e in liste]
    assert positions[0] > noms.index("mp1")
    assert positions[0] < noms.index("montage /etc/pgsql-git")


def test_les_unites_sont_rechargees_avant_dARMER_un_timer(ctx):
    """Armer une unité que systemd n'a pas relue arme la précédente version —
    et c'est le genre de défaut qui ne se voit qu'à 2h30."""
    liste = etapes(ctx)
    noms = [e.name for e in liste]
    barrieres = [i for i, e in enumerate(liste) if isinstance(e, Barrier)]
    for timer in ("pg-backup.timer (armement)", "pgbk-offsite.timer (armement)"):
        rang = noms.index(timer)
        assert any(b < rang for b in barrieres)


def test_la_premiere_sauvegarde_precede_le_hors_site(ctx):
    """Sans elle, la copie initiale n'aurait rien à transférer et sortirait en
    erreur « aucune sauvegarde locale ». L'ordre n'est pas cosmétique."""
    noms = [e.name for e in etapes(ctx)]
    assert noms.index("première sauvegarde") < noms.index(
        "pgbk-offsite.timer (armement)")


def test_les_controles_ferment_le_parcours(ctx):
    """Ils constatent ce que le déploiement vient de faire : les jouer avant
    répondrait sur l'état d'avant."""
    liste = etapes(ctx)
    derniere_pose = max(
        i for i, e in enumerate(liste) if getattr(e, "section", "") != "C"
    )
    assert all(e.section == "C" for e in liste[derniere_pose + 1:])
    assert liste[-1].section == "C"


def test_le_moteur_python_precede_son_lanceur(ctx):
    """Un lanceur sans arbre d'import échoue sur un ImportError qui ne dit rien
    de la vraie cause."""
    noms = [e.name for e in etapes(ctx)]
    assert noms.index("moteur (CT)") < noms.index("pg (CT)")


def test_sans_conteneur_les_etapes_du_CT_sont_sautees(ctx):
    """Un drapeau ne désactive jamais un contrôle, seulement une pose : les
    étapes restent dans la liste et se déclarent sautées."""
    ctx.opts = Options(ctid=200, do_container=False)
    liste = etapes(ctx)
    par_nom = {e.name: e for e in liste}
    assert par_nom["mp1"].skip_if(ctx) is not None
    assert par_nom["pg_hba"].skip_if(ctx) is None, "un contrôle reste un contrôle"


def test_sans_hors_site_le_reste_du_deploiement_tient(ctx):
    ctx.opts = Options(ctid=200, do_offsite=False)
    par_nom = {e.name: e for e in etapes(ctx)}
    assert par_nom["pgbk-offsite.timer (armement)"].skip_if(ctx) is not None
    assert par_nom["pg-backup.timer (armement)"].skip_if(ctx) is None
