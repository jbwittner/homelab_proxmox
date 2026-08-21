"""Extraire d'une sortie d'erreur la ligne qui DIT quelque chose.

DÉFAUT CONSTATÉ LE 21 AOÛT 2026, sur `pve-eranikus` :

    KO  connexion à la base (CT 200) — Forgejo ne joint pas sa base sur le
        CT 200 — perl: warning: Setting locale failed.

    KO  base (CT 200)    perl: warning: Setting locale failed.

Ce n'est pas la cause de l'échec. `pct` est un programme **Perl** : quand la
locale de la session n'existe pas dans le conteneur, il émet ses propres
avertissements sur la sortie d'erreur, AVANT que la commande distante n'ait
écrit quoi que ce soit. Prendre la première ligne, c'est donc lire `pct` et
non ce qu'on a lancé.

Le message envoyait regarder les locales du conteneur, alors que le vrai
message — un `FATAL:` de PostgreSQL — était plus bas et disait exactement quoi
corriger. Le pire genre de message d'erreur : plausible, précis, et à côté.
"""

from __future__ import annotations

import pytest

from core.runner import ligne_utile

# La sortie RÉELLE, recopiée telle quelle.
BRUIT_PCT = (
    "perl: warning: Setting locale failed.\n"
    "perl: warning: Please check that your locale settings:\n"
    '\tLANGUAGE = (unset),\n'
    '\tLC_ALL = (unset),\n'
    '\tLANG = "fr_FR.UTF-8"\n'
    "    are supported and installed on your system.\n"
    "perl: warning: Falling back to the standard locale (\"C\").\n"
)


def test_le_bruit_de_pct_nest_pas_pris_pour_la_cause():
    """LE défaut. Sans le vrai message, on ne doit surtout pas rendre le
    bruit : mieux vaut avouer qu'on n'a rien qu'affirmer une fausse piste."""
    assert "perl" not in ligne_utile(BRUIT_PCT).lower()


def test_le_fatal_de_postgresql_lemporte_sur_le_bruit():
    """Le cas qui compte : la cause EST dans la sortie, mais après le bruit."""
    sortie = BRUIT_PCT + 'psql: error: connection to server failed\n' \
        'FATAL:  no pg_hba.conf entry for host "192.168.1.57"\n'
    assert 'no pg_hba.conf entry' in ligne_utile(sortie)


@pytest.mark.parametrize(
    "cause",
    [
        'FATAL:  password authentication failed for user "forgejo"',
        'FATAL:  database "forgejo" does not exist',
        "psql: error: connection to server at \"192.168.1.56\" failed",
        "could not connect to server: Connection refused",
    ],
)
def test_les_causes_reelles_sont_reconnues(cause):
    assert ligne_utile(BRUIT_PCT + cause + "\n") == cause


def test_sans_rien_de_significatif_la_derniere_ligne_utile_gagne():
    """Un outil inconnu n'écrira pas « FATAL ». La dernière ligne non vide est
    le meilleur pari restant : c'est presque toujours le verdict, le bruit
    venant en tête."""
    sortie = BRUIT_PCT + "quelque chose a mal tourné\n"
    assert ligne_utile(sortie) == "quelque chose a mal tourné"


def test_une_sortie_vide_le_dit_plutot_que_dinventer():
    """Rendre une chaîne vide laisserait un message tronqué du genre
    « Forgejo ne joint pas sa base — ». Autant le dire."""
    assert ligne_utile("") == "aucun message"
    assert ligne_utile("   \n\n") == "aucun message"


def test_une_sortie_sans_bruit_est_rendue_telle_quelle():
    assert ligne_utile("FATAL:  rôle inconnu\n") == "FATAL:  rôle inconnu"


def test_le_bruit_seul_le_dit_sans_le_recopier():
    """Quand il n'y a QUE du bruit, la réponse honnête est « rien d'utile »,
    accompagnée d'un indice sur ce qui l'a produit — sinon on cherche un
    message qui n'existe pas."""
    rendu = ligne_utile(BRUIT_PCT)
    assert "aucun message" in rendu
    assert "locale" in rendu, "dire d'où venait le bruit aide à ne pas le craindre"
