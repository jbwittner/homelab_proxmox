"""L'épinglage de la clé de signature.

La propriété défendue ici est unique et tient en une phrase : **une clé qui a
changé doit produire un REFUS, pas une vérification qui passe.**

Vérifier une signature contre n'importe quelle clé ne prouve rien — cela
montre seulement que celui qui a fabriqué l'artefact savait aussi le signer.
C'est l'épinglage qui donne du sens à la vérification : l'artefact vient de la
même origine que la dernière fois, et cette origine a été approuvée une fois,
dans un commit qu'on peut relire.
"""

from __future__ import annotations

import pytest

from fjtool import cle as K

# Une empreinte GPG v4 plausible, et sa forme « à espaces » telle que gpg
# l'affiche à l'écran — donc telle qu'un humain la recopie.
FPR = "ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234"
FPR_ESPACES = "ABCD 1234 ABCD 1234 ABCD  1234 ABCD 1234 ABCD 1234"


# ─── normalisation ───────────────────────────────────────────────────────────


def test_la_forme_a_espaces_est_acceptee():
    """`gpg --fingerprint` affiche par groupes de quatre. Refuser cette forme
    obligerait à retaper une valeur qu'on vient de lire à l'écran."""
    assert K.normaliser(FPR_ESPACES) == FPR


def test_la_casse_est_indifferente():
    assert K.normaliser(FPR.lower()) == FPR


def test_deux_ecritures_de_la_meme_cle_sont_egales():
    """Le point qui compte : sans normalisation, la même clé écrite de deux
    façons se lirait comme deux clés — donc comme un changement de clé, donc
    comme un refus, sur une installation parfaitement saine."""
    assert K.normaliser(FPR_ESPACES) == K.normaliser(FPR.lower())


# ─── validation ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mauvaise",
    [
        "ABCD1234",                                    # trop courte
        FPR + "AB",                                    # trop longue
        "ZZZZ1234ABCD1234ABCD1234ABCD1234ABCD1234",    # pas hexadécimal
        "",                                            # vide
    ],
)
def test_une_empreinte_mal_formee_est_refusee(mauvaise):
    with pytest.raises(K.CleError):
        K.valider(mauvaise)


def test_une_empreinte_bien_formee_passe():
    assert K.valider(FPR_ESPACES) == FPR


# ─── lecture du fichier ──────────────────────────────────────────────────────


def test_les_commentaires_sont_ignores():
    assert K.parse(f"# un commentaire\n\n{FPR}\n") == FPR


def test_un_fichier_sans_empreinte_rend_none():
    """`None` veut dire « non épinglée », et jamais « n'importe laquelle » —
    c'est la distinction que tout ce module existe pour tenir."""
    assert K.parse("# rien que des commentaires\n\n") is None


def test_lire_un_fichier_absent_rend_none(tmp_path):
    assert K.lire(tmp_path / "RELEASE-KEY.fingerprint") is None


def test_lire_normalise(tmp_path):
    chemin = tmp_path / "f"
    chemin.write_text(f"# en-tête\n{FPR_ESPACES}\n")
    assert K.lire(chemin) == FPR


def test_le_fichier_du_depot_est_lisible_par_le_meme_analyseur(depot_forgejo):
    """Le fichier livré doit passer par le même chemin que n'importe quel
    autre. Il peut être non épinglé — ce qui compte est que la lecture
    aboutisse et que ce qui s'y trouve, s'il y a quelque chose, soit valide."""
    chemin = depot_forgejo / "ct" / "RELEASE-KEY.fingerprint"
    assert chemin.is_file(), "le fichier doit exister, même non épinglé"
    valeur = K.lire(chemin)
    if valeur is not None:
        assert K.valider(valeur) == valeur


# ─── rendu ───────────────────────────────────────────────────────────────────


def test_le_fichier_rendu_se_relit_lui_meme():
    """Aller-retour. Sans ce contrôle, un en-tête mal formé rendrait
    l'empreinte invisible au déploiement suivant, qui refuserait d'installer
    sans que personne comprenne pourquoi."""
    assert K.parse(K.rendre(FPR, source="https://exemple/clé")) == FPR


def test_le_rendu_consigne_la_source():
    """« D'où vient cette clé » est la première question six mois plus tard,
    et quarante caractères hexadécimaux n'y répondent pas."""
    assert "https://exemple/clé" in K.rendre(FPR, source="https://exemple/clé")


def test_le_rendu_garde_l_avertissement():
    texte = K.rendre(FPR, source="x")
    assert "ÉPINGLÉE" in texte
    assert "DÉCISION" in texte


# ─── la décision, et c'est tout le module ────────────────────────────────────


def test_sans_epinglage_on_retient_la_premiere():
    """Un bloc porte souvent plusieurs empreintes — une clé principale et ses
    sous-clés. À l'amorçage, la principale est celle que gpg rend en tête."""
    assert K.retenir([FPR, "1111" * 10], epinglee=None) == FPR


def test_avec_epinglage_on_retient_celle_qui_correspond():
    """Une sous-clé ajoutée au bloc ne doit pas faire échouer un déploiement :
    ce qui compte est que la clé approuvée soit TOUJOURS là."""
    autre = "1111" * 10
    assert K.retenir([autre, FPR], epinglee=FPR) == FPR


def test_une_cle_qui_ne_correspond_plus_est_un_refus():
    """LE test de ce module. Sans lui, une clé de signature remplacée passerait
    inaperçue, et la vérification de signature ne prouverait plus rien."""
    with pytest.raises(K.CleError) as capture:
        K.retenir(["1111" * 10], epinglee=FPR)
    message = str(capture.value)
    assert "Rien n'est installé" in message
    assert FPR in message, "le refus doit montrer ce qui était épinglé"
    assert "1111" in message, "et ce qui a été trouvé"


def test_le_refus_dit_les_deux_explications_possibles():
    """Un changement de clé légitime et une source falsifiée produisent le même
    symptôme. Le message doit nommer les deux, sinon on conclut trop vite."""
    with pytest.raises(K.CleError) as capture:
        K.retenir(["1111" * 10], epinglee=FPR)
    message = str(capture.value)
    assert "changé de clé" in message
    assert "n'est pas celle qu'on croit" in message


def test_la_comparaison_ignore_la_mise_en_forme():
    """Épinglée à espaces, trouvée sans : c'est la même clé."""
    assert K.retenir([FPR], epinglee=K.normaliser(FPR_ESPACES)) == FPR


# ─── récupération ────────────────────────────────────────────────────────────


def test_un_fichier_local_est_une_source_valable(tmp_path):
    """Ce n'est pas une commodité : c'est ce qui permet d'apporter la clé par
    le moyen qu'on veut — une autre machine, une clé USB — sans que ce code
    n'ait à connaître ce moyen."""
    source = tmp_path / "cle.asc"
    source.write_bytes(b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n")
    assert K.recuperer(str(source)).startswith(b"-----BEGIN")


def test_une_source_qui_nest_ni_fichier_ni_url_est_refusee(tmp_path):
    """Sans ce refus, une faute de frappe partirait en requête réseau vers un
    nom d'hôte inventé, et l'erreur parlerait de DNS."""
    with pytest.raises(K.CleError) as capture:
        K.recuperer(str(tmp_path / "jamais.asc"))
    assert "ni un fichier existant ni une URL" in str(capture.value)
