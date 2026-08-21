"""Les décisions des étapes, éprouvées sans infrastructure.

Chaque test ici porte sur une décision que l'étape prend seule : un ACL qui
laisse ou non passer PUBLIC, un `app.ini` qui déclare ou non un proxy de
confiance, une somme de contrôle lue au bon endroit. Ce sont les endroits où
une erreur ne produit AUCUN message — juste une protection qui n'est plus là.
"""

from __future__ import annotations

import pytest

from core.converge import Mode
from core.runner import FakeRunner, Result
from fjtool.deploy import Options, Paths, contexte
from fjtool.steps import binaire as V
from fjtool.steps import controles as C
from fjtool.steps import retraits as H


@pytest.fixture
def ctx(depot_forgejo):
    return contexte(
        runner=FakeRunner(),
        paths=Paths(src=depot_forgejo),
        opts=Options(ctid=400),
        mode=Mode.STATUS,
    )


def _repond(ctx, fragment: str, sortie: str = "", code: int = 0):
    ctx.runner.when(fragment, Result((fragment,), code, sortie, ""))


# ─── le proxy de confiance ───────────────────────────────────────────────────


def _app_ini(ctx, contenu: str):
    ctx.runner.when(
        "cat /etc/forgejo/app.ini",
        Result(("cat",), 0, contenu, ""),
    )


def test_un_marqueur_de_gabarit_est_signale_comme_jamais_renseigne(ctx):
    """`@@TRAEFIK_IP@@` resté en place n'est pas « une valeur trop large » :
    c'est une configuration jamais remplie, et le message doit le dire pour
    que la correction soit évidente."""
    _app_ini(ctx, "REVERSE_PROXY_TRUSTED_PROXIES = @@TRAEFIK_IP@@\n")
    resultat = C.ProxyDeConfiance().check(ctx)
    assert resultat.state == "error"
    assert "jamais été renseigné" in resultat.detail
    assert "ct/app.ini" in resultat.detail, "le refus doit dire où corriger"


def test_un_joker_est_refuse(ctx):
    """Avec `*`, n'importe quel client du LAN se déclare n'importe quelle
    adresse par X-Forwarded-For : journaux et limitations par IP ne veulent
    plus rien dire."""
    _app_ini(ctx, "REVERSE_PROXY_TRUSTED_PROXIES = *\n")
    resultat = C.ProxyDeConfiance().check(ctx)
    assert resultat.state == "error"
    assert "joker" in resultat.detail


def test_une_ip_explicite_passe(ctx):
    _app_ini(ctx, "REVERSE_PROXY_TRUSTED_PROXIES = 192.168.1.53\n")
    assert C.ProxyDeConfiance().check(ctx).state == "ok"


def test_une_valeur_vide_est_refusee(ctx):
    _app_ini(ctx, "REVERSE_PROXY_TRUSTED_PROXIES =\n")
    assert C.ProxyDeConfiance().check(ctx).state == "error"


# ─── l'inscription publique ──────────────────────────────────────────────────


def test_install_lock_et_inscription_sont_regardes_ensemble(ctx):
    """Les deux ferment la même porte par deux chemins : l'un empêche
    l'assistant web d'adopter l'instance, l'autre empêche un visiteur de s'y
    créer un compte. Un seul des deux ne suffit pas."""
    _app_ini(ctx, "INSTALL_LOCK = true\nDISABLE_REGISTRATION = false\n")
    resultat = C.InscriptionFermee().check(ctx)
    assert resultat.state == "error"
    assert "DISABLE_REGISTRATION" in resultat.detail
    assert "INSTALL_LOCK" not in resultat.detail, "seul l'écart doit être nommé"


def test_les_deux_verrous_poses_donnent_ok(ctx):
    _app_ini(ctx, "INSTALL_LOCK = true\nDISABLE_REGISTRATION = true\n")
    assert C.InscriptionFermee().check(ctx).state == "ok"


def test_une_cle_absente_est_traitee_comme_un_ecart(ctx):
    """Une clé absente vaut le DÉFAUT de Forgejo, pas « déjà fermé »."""
    _app_ini(ctx, "APP_NAME = Forgejo\n")
    resultat = C.InscriptionFermee().check(ctx)
    assert resultat.state == "error"
    assert "absent" in resultat.detail


# ─── lecture d'un app.ini ────────────────────────────────────────────────────


def test_lire_ini_aplatit_les_sections():
    """Les clés qui nous intéressent sont uniques dans tout le fichier ;
    suivre les sections n'apporterait que des occasions de se tromper de nom
    de section au fil des versions de Forgejo."""
    texte = (
        "; un commentaire\n"
        "APP_NAME = Forgejo\n"
        "[security]\n"
        "INSTALL_LOCK = true\n"
        "[cron.update_checker]\n"
        "ENABLED = false\n"
    )
    reglages = C.lire_ini(texte)
    assert reglages["INSTALL_LOCK"] == "true"
    assert reglages["ENABLED"] == "false"
    assert "[security]" not in reglages


def test_lire_ini_ignore_les_deux_styles_de_commentaire():
    """app.ini admet `;` et `#`. En manquer un ferait lire un commentaire
    comme un réglage — et un « ; DISABLE_REGISTRATION = false » mis en
    commentaire passerait pour actif."""
    reglages = C.lire_ini("; a = 1\n# b = 2\nc = 3\n")
    assert reglages == {"c": "3"}


def test_le_app_ini_du_depot_ferme_bien_les_deux_portes(depot_forgejo):
    """Le fichier RÉELLEMENT livré, confronté au contrôle qui le juge.

    C'est le test qui empêche `ct/app.ini` et `steps/controles.py` de diverger
    en silence : si quelqu'un retire `DISABLE_REGISTRATION` du fichier, ce
    test rougit ici, et pas au premier visiteur qui s'inscrit.
    """
    reglages = C.lire_ini((depot_forgejo / "ct" / "app.ini").read_text())
    for cle, attendu in C.InscriptionFermee.ATTENDUS.items():
        assert reglages.get(cle, "").lower() == attendu, f"{cle} dans ct/app.ini"
    assert reglages["ALLOW_LOCALNETWORKS"].lower() == "false"
    assert reglages["DB_TYPE"] == "postgres"
    # La base vit dans le CT 200 : TCP vers son IP, et SSL parce que le
    # transport traverse le LAN.
    assert reglages["HOST"] == "192.168.1.56:5432"
    assert reglages["SSL_MODE"] == "require"
    # Le mot de passe est SUBSTITUÉ à la pose : le gabarit du dépôt doit
    # porter le marqueur, jamais une valeur.
    assert reglages["PASSWD"] == "@@DB_PASSWORD@@", (
        "aucun mot de passe en clair dans le dépôt"
    )


# ─── la somme de contrôle ────────────────────────────────────────────────────


def test_la_somme_est_lue_dans_le_premier_champ():
    """Le format est « <hex>  <nom> ». Le nom varie d'une publication à
    l'autre, l'empreinte non : ne prendre que le premier champ."""
    texte = "abc123  forgejo-15.0.3-linux-amd64\n"
    assert V.somme_attendue(texte) == "abc123"


def test_un_fichier_de_somme_vide_leve():
    """Un `.sha256` vide ne doit pas produire une empreinte vide qui
    comparerait « égal » à une autre empreinte vide."""
    with pytest.raises(Exception):
        V.somme_attendue("   \n")


# ─── le durcissement git ─────────────────────────────────────────────────────


def test_les_trois_reglages_fsck_sont_exiges(ctx):
    """Trois chemins d'entrée d'objets, trois réglages. En poser deux laisse
    la troisième porte ouverte, et c'est toujours celle-là qui sert."""
    etape = V.DurcissementGit()
    assert set(etape.REGLAGES) == {
        "transfer.fsckObjects", "receive.fsckObjects", "fetch.fsckObjects"
    }


def test_un_reglage_fsck_manquant_est_pose(ctx):
    ctx.runner.when("git config --system --get transfer.fsckObjects",
                    Result(("git",), 0, "true\n", ""))
    ctx.runner.when("git config --system --get receive.fsckObjects",
                    Result(("git",), 0, "true\n", ""))
    ctx.runner.when("git config --system --get fetch.fsckObjects",
                    Result(("git",), 1, "", ""))
    resultat = V.DurcissementGit().check(ctx)
    assert resultat.state == "drift"
    assert len(resultat.actions) == 1
    assert "fetch.fsckObjects" in resultat.actions[0].label


def test_les_trois_poses_donnent_ok(ctx):
    ctx.runner.when("git config --system --get",
                    Result(("git",), 0, "true\n", ""))
    assert V.DurcissementGit().check(ctx).state == "ok"


# ─── l'automatisme de mise à jour, qui ne doit pas exister ───────────────────


def test_aucun_automatisme_trouve_donne_ok(ctx):
    ctx.runner.when("for f in", Result(("sh",), 0, "", ""))
    assert H.AucunAutoUpdate().check(ctx).state == "ok"


def test_un_chemin_de_mise_a_jour_est_une_erreur_sans_action(ctx):
    """Signalé, JAMAIS corrigé en silence : un automatisme qu'on n'a pas posé
    soi-même est la trace de quelque chose à comprendre avant d'effacer."""
    ctx.runner.when("for f in", Result(("sh",), 0, "/usr/bin/update\n", ""))
    resultat = H.AucunAutoUpdate().check(ctx)
    assert resultat.state == "error"
    assert not resultat.actions, "rien ne doit être supprimé automatiquement"
    assert "/usr/bin/update" in resultat.detail
    assert "irréversible" in resultat.detail
