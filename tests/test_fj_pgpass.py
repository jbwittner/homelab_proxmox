"""Le mot de passe traverse un fichier `.pgpass`, dont le format a deux pièges.

Une ligne de `.pgpass` est `hôte:port:base:utilisateur:motdepasse`. Les
DEUX-POINTS y séparent les champs, et l'ANTISLASH échappe. Un mot de passe qui
en contient casse donc la ligne — psql lit un mot de passe tronqué, le serveur
refuse, et le message est :

    FATAL:  password authentication failed for user "forgejo"

C'est-à-dire **exactement le message d'un mauvais mot de passe**. On part alors
vérifier le secret, le recopier, le regénérer — et rien n'y fait, puisque le
secret est juste.

`pg deploy --tenant` produit des mots de passe alphanumériques, donc le cas ne
se pose pas aujourd'hui. Mais un mot de passe posé à la main par un `ALTER
ROLE`, ou repris d'un gestionnaire, n'a aucune raison de l'être — et la panne
qui en résulterait ne ressemblerait pas à sa cause.
"""

from __future__ import annotations

import subprocess

import pytest

from fjtool.steps.postgres import ECHAPPE_PGPASS


def _echappe(motdepasse: str) -> str:
    """Fait tourner POUR DE VRAI le filtre shell embarqué dans la sonde.

    Le tester en réimplémentant l'échappement en Python ne prouverait rien :
    ce qui compte est ce que `sed` fait, pas ce qu'on croit qu'il fait.
    """
    res = subprocess.run(
        ["sh", "-c", f'printf %s "$1" | {ECHAPPE_PGPASS}', "sh", motdepasse],
        capture_output=True, text=True, check=True,
    )
    return res.stdout


@pytest.mark.parametrize(
    "brut,attendu",
    [
        ("simple123", "simple123"),
        ("avec:deuxpoints", r"avec\:deuxpoints"),
        ("avec\\antislash", r"avec\\antislash"),
        # L'ordre compte : l'antislash D'ABORD, sinon celui que l'on vient
        # d'ajouter pour les deux-points serait échappé à son tour.
        ("a:b\\c", r"a\:b\\c"),
        ("::", r"\:\:"),
    ],
)
def test_les_caracteres_speciaux_du_format_sont_echappes(brut, attendu):
    assert _echappe(brut) == attendu


def test_un_mot_de_passe_alphanumerique_traverse_intact():
    """Celui que `pg deploy --tenant` produit : rien ne doit lui arriver."""
    brut = "Xk29fQz7Lm4pRt8vBn3wJdY6hCsA1eUg"
    assert _echappe(brut) == brut


def test_le_filtre_est_bien_celui_quembarque_la_sonde():
    """Sans ce contrôle, on pourrait corriger l'échappement dans le test et
    laisser la sonde inchangée — le test passerait, la production non."""
    from fjtool.steps import postgres

    for source in (postgres.ConnexionBase.__dict__.get("check").__doc__ or "",):
        pass
    import inspect

    code = inspect.getsource(postgres.ConnexionBase.check)
    assert "ECHAPPE_PGPASS" in code or ECHAPPE_PGPASS in code, (
        "la sonde doit utiliser le filtre, pas une copie divergente"
    )
