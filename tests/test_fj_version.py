"""L'épinglage de version — la raison d'être de ce service.

Ces tests défendent une seule propriété, sous plusieurs angles : **« non
résolue » ne doit jamais pouvoir se transformer en « la dernière »**. C'est
exactement ce que fait le script communautaire avec son `"latest"` en dur, et
ce que ce module existe pour empêcher.
"""

from __future__ import annotations

import pytest

from fjtool import version as V


# ─── lecture du fichier ──────────────────────────────────────────────────────


def test_les_commentaires_et_les_lignes_vides_sont_ignores():
    texte = "# un commentaire\n\n   \n# encore\nv15.0.3\n"
    assert V.parse(texte) == "v15.0.3"


def test_un_fichier_sans_version_rend_none_et_non_une_chaine_vide():
    """`None` veut dire « non résolue ». Une chaîne vide se comparerait à
    l'égal d'autre chose et finirait par passer pour une valeur."""
    assert V.parse("# rien que des commentaires\n\n") is None


def test_lire_un_fichier_absent_rend_none(tmp_path):
    assert V.lire(tmp_path / "VERSION") is None


def test_le_fichier_du_depot_est_lisible_par_le_meme_analyseur(depot_forgejo):
    """Le fichier livré doit passer par le même chemin que n'importe quel
    autre — sinon rien ne garantit qu'il sera compris le jour venu."""
    chemin = depot_forgejo / "ct" / "VERSION"
    assert chemin.is_file(), "ct/VERSION doit exister, même non résolu"
    # Peut être None (non résolu) ; ce qui compte est que la lecture aboutisse.
    valeur = V.lire(chemin)
    if valeur is not None:
        assert V.valider(valeur) == valeur


# ─── validation ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mauvaise",
    [
        "15.0.3",       # sans le « v »
        "v15.0",        # sans correctif
        "v15.0.3-rc1",  # pré-version déguisée en tag propre
        "latest",       # LE mot que ce module existe pour refuser
        "v15.0.3 ",     # espace en fin : invisible à la relecture
        "",             # fichier vide
    ],
)
def test_une_version_mal_formee_est_refusee(mauvaise):
    with pytest.raises(V.VersionError):
        V.valider(mauvaise)


def test_une_version_hors_branche_lts_est_refusee():
    """LE contrôle du module. Une 16.0 collée à la main dans VERSION est
    arrêtée ici, et nulle part ailleurs."""
    with pytest.raises(V.VersionError) as capture:
        V.valider("v16.0.0")
    assert "branche LTS" in str(capture.value)


def test_une_version_de_la_branche_epinglee_passe():
    assert V.valider("v15.0.7") == "v15.0.7"


# ─── tri ─────────────────────────────────────────────────────────────────────


def test_le_tri_est_numerique_et_non_lexicographique():
    """« v15.0.10 » est plus récent que « v15.0.9 ».

    Un tri de chaînes conclurait l'inverse, et ce jour-là on installerait une
    version PLUS ANCIENNE en croyant faire l'inverse — sans qu'aucun message
    ne le signale, puisque les deux sont sur la branche épinglée.
    """
    tags = ["v15.0.9", "v15.0.10", "v15.0.2"]
    assert max(tags, key=V.cle_de_tri) == "v15.0.10"
    assert max(tags) == "v15.0.9", "le tri de chaînes se trompe — d'où le tri numérique"


# ─── sélection d'une publication ─────────────────────────────────────────────


def _publication(tag, *, draft=False, prerelease=False):
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease}


def test_retenir_prend_la_plus_recente_de_la_branche():
    retenue = V.retenir([
        _publication("v15.0.1"),
        _publication("v15.0.3"),
        _publication("v15.0.2"),
    ])
    assert retenue.tag == "v15.0.3"


def test_retenir_ecarte_les_brouillons_et_les_preversions():
    """Un brouillon peut voir ses artefacts changer sous le même tag ; une
    pré-version n'est pas destinée à une source de vérité."""
    retenue = V.retenir([
        _publication("v15.0.1"),
        _publication("v15.0.9", draft=True),
        _publication("v15.0.8", prerelease=True),
    ])
    assert retenue.tag == "v15.0.1"


def test_retenir_ecarte_les_autres_branches():
    """C'est ici que la 16.0 est écartée, alors même qu'elle est la plus
    récente — c'est tout le sujet."""
    retenue = V.retenir([
        _publication("v15.0.1"),
        _publication("v16.0.0"),
        _publication("v17.0.0"),
    ])
    assert retenue.tag == "v15.0.1"


def test_retenir_leve_si_la_branche_na_plus_rien():
    """Plutôt que de retomber sur une autre branche « pour que ça marche ».

    Le jour où 15.0 disparaît des publications est le jour où il faut
    DÉCIDER, pas celui où l'outil décide tout seul.
    """
    with pytest.raises(V.VersionError) as capture:
        V.retenir([_publication("v16.0.0")])
    assert "15.0" in str(capture.value)


def test_un_tag_de_la_branche_mais_mal_forme_est_ecarte():
    """« v15.0.3-1 » commence bien par le préfixe, et n'est pourtant pas une
    version : le préfixe seul ne suffit donc pas à retenir."""
    retenue = V.retenir([
        _publication("v15.0.3-1"),
        _publication("v15.0.2"),
    ])
    assert retenue.tag == "v15.0.2"


# ─── noms d'artefacts ────────────────────────────────────────────────────────


def test_les_urls_derivent_du_tag_sans_le_v():
    release = V.Release("v15.0.3")
    assert release.binaire == "forgejo-15.0.3-linux-amd64"
    assert release.url().endswith("/v15.0.3/forgejo-15.0.3-linux-amd64")
    assert release.url(".asc").endswith(".asc")
    assert release.url(".sha256").endswith(".sha256")


# ─── ce que le binaire déclare ───────────────────────────────────────────────


def test_la_version_installee_ignore_le_suffixe_de_compatibilite():
    """« Forgejo version 15.0.3+gitea-1.22.0 » → « v15.0.3 ».

    Comparer la chaîne entière annoncerait une dérive à chaque déploiement :
    le suffixe Gitea bouge sans que la version de Forgejo bouge.
    """
    sortie = "Forgejo version 15.0.3+gitea-1.22.0 built with go1.24"
    assert V.version_installee(sortie) == "v15.0.3"


def test_la_version_installee_accepte_le_v_optionnel():
    assert V.version_installee("Forgejo version v15.0.3 built") == "v15.0.3"


def test_une_sortie_muette_rend_none():
    """Un binaire absent ou cassé ne doit pas ressembler à une version."""
    assert V.version_installee("") is None
    assert V.version_installee("command not found") is None


# ─── rendu du fichier ────────────────────────────────────────────────────────


def test_le_fichier_rendu_se_relit_lui_meme():
    """Aller-retour : ce qu'on écrit doit être ce qu'on relit. Sans ce
    contrôle, un en-tête mal formé rendrait la version invisible au
    déploiement suivant, qui refuserait d'installer sans dire pourquoi."""
    texte = V.rendre("v15.0.3")
    assert V.parse(texte) == "v15.0.3"


def test_le_fichier_rendu_garde_son_avertissement():
    """L'avertissement ne doit pas se perdre au fil des résolutions : c'est la
    seule chose qui explique, dans six mois, pourquoi ce CT n'est pas à jour."""
    texte = V.rendre("v15.0.3")
    assert "JAMAIS MIS À JOUR PAR UN SCRIPT COMMUNAUTAIRE" in texte
    assert V.EOL in texte
