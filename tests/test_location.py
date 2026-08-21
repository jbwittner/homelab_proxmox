"""La façade : détection, résolution du CTID, gardes et confirmations.

Rien n'est exécuté : `FakeRunner` répond à la place de `pct`, et la saisie
clavier est injectée. Ce fichier teste les décisions, pas les commandes.
"""

from __future__ import annotations

import pytest

from core.runner import CommandError, FakeRunner, InContainer, Result
from pgtool.location import (
    CT_PGBK,
    Delegate,
    Refus,
    Where,
    confirm,
    detect,
    first_positional,
    read_conf,
    resolve_ctid,
)


# ─── détection ───────────────────────────────────────────────────────────────


def test_le_noeud_se_reconnait_a_pct(monkeypatch):
    """Même critère que le bash : le changer casserait la cohabitation des
    deux implémentations pendant la migration."""
    r = FakeRunner()
    monkeypatch.setattr("shutil.which", lambda nom: "/usr/sbin/pct" if nom == "pct" else None)
    assert detect(r) is Where.HOST


def test_le_conteneur_na_pas_pct(monkeypatch):
    r = FakeRunner()
    monkeypatch.setattr("shutil.which", lambda nom: None)
    assert detect(r) is Where.CONTAINER


# ─── /etc/default/pgbk ───────────────────────────────────────────────────────


def test_le_fichier_est_analyse_jamais_execute(tmp_path):
    """Le bash faisait un `source`, qui exécute. Un fichier de configuration
    n'a pas à pouvoir lancer des commandes."""
    f = tmp_path / "pgbk"
    f.write_text(
        "# Genere par pg deploy\n"
        "PG_CTID=200\n"
        "PAYLOAD=$(touch /tmp/preuve)\n"
    )
    valeurs = read_conf(f)
    assert valeurs["PG_CTID"] == "200"
    assert valeurs["PAYLOAD"] == "$(touch /tmp/preuve)", "conservé comme texte"


def test_un_fichier_absent_nest_pas_une_erreur(tmp_path):
    assert read_conf(tmp_path / "absent") == {}


def test_les_guillemets_sont_retires(tmp_path):
    f = tmp_path / "pgbk"
    f.write_text('PG_CTID="201"\n')
    assert read_conf(f)["PG_CTID"] == "201"


# ─── résolution du CTID ──────────────────────────────────────────────────────


def test_le_drapeau_prime_sur_tout():
    assert resolve_ctid(flag="299", env={"PG_CTID": "200"}, conf={"PG_CTID": "201"}) == 299


def test_lenvironnement_prime_sur_le_fichier():
    """Le bash capture PG_CTID AVANT de sourcer le fichier, précisément pour
    que l'environnement l'emporte."""
    assert resolve_ctid(flag=None, env={"PG_CTID": "200"}, conf={"PG_CTID": "201"}) == 200


def test_le_fichier_sert_de_dernier_recours():
    assert resolve_ctid(flag=None, env={}, conf={"PG_CTID": "201"}) == 201


def test_aucun_ctid_est_un_refus_pas_un_defaut():
    """Deviner un CTID, c'est risquer de restaurer une base dans le mauvais
    conteneur. `pg deploy` a un défaut, pas cette façade."""
    with pytest.raises(Refus) as exc:
        resolve_ctid(flag=None, env={}, conf={})
    assert "aucun conteneur cible" in str(exc.value)
    assert "pg deploy --ctid" in str(exc.value), "le message doit dire quoi faire"


def test_un_ctid_non_numerique_est_refuse():
    with pytest.raises(Refus, match="CTID invalide"):
        resolve_ctid(flag="deux-cents", env={}, conf={})


def test_un_ctid_partiellement_numerique_est_refuse():
    with pytest.raises(Refus, match="CTID invalide"):
        resolve_ctid(flag="200; rm -rf /", env={}, conf={})


# ─── gardes avant délégation ─────────────────────────────────────────────────


def _noeud(**reponses) -> FakeRunner:
    r = FakeRunner()
    for fragment, res in reponses.items():
        r.when(fragment, res)
    return r


def test_preflight_passe_sur_un_ct_pret():
    r = _noeud()
    r.when(lambda a: a[:2] == ("pct", "status"), Result(("pct",), 0, "status: running\n", ""))
    Delegate(r, 200).preflight()


def test_un_ct_inexistant_est_dit_avant_de_deleguer():
    """Plutôt qu'un « command not found » venu de l'autre côté du montage, que
    rien ne rattache à sa cause."""
    r = _noeud()
    r.when(lambda a: a[:2] == ("pct", "config"), Result(("pct",), 1, "", ""))
    with pytest.raises(Refus, match="inexistant"):
        Delegate(r, 200).preflight()


def test_un_ct_arrete_dit_comment_le_demarrer():
    r = _noeud()
    r.when(lambda a: a[:2] == ("pct", "status"), Result(("pct",), 0, "status: stopped\n", ""))
    with pytest.raises(Refus) as exc:
        Delegate(r, 200).preflight()
    assert "pct start 200" in str(exc.value)


def test_un_moteur_absent_renvoie_au_deploiement():
    r = _noeud()
    r.when(lambda a: a[:2] == ("pct", "status"), Result(("pct",), 0, "status: running\n", ""))
    r.when(lambda a: "test" in a, Result(("pct",), 1, "", ""))
    with pytest.raises(Refus) as exc:
        Delegate(r, 200).preflight()
    assert CT_PGBK in str(exc.value) and "pg deploy" in str(exc.value)


# ─── --plan : la question porte sur ce qui sera supprimé ─────────────────────


def test_plan_rend_le_nom_resolu():
    """Le conteneur seul sait que « 20260819 » désigne la plus récente de ce
    jour-là."""
    r = _noeud()
    r.when("--plan", Result(("pct",), 0, "20260819-233627\n", "détail humain"))
    assert Delegate(r, 200).plan("delete", ["20260819"]) == "20260819-233627"


def test_plan_neffacerien():
    r = _noeud()
    r.when("--plan", Result(("pct",), 0, "20260819-233627\n", ""))
    Delegate(r, 200).plan("delete", ["20260819"])
    assert all("--yes" not in argv for argv in r.calls)
    assert len(r.calls) == 1


def test_un_refus_du_moteur_remonte_sans_supprimer(capsys):
    """Une garde a parlé — le dernier instantané est protégé, par exemple.
    Son message est déjà passé, il ne faut pas le doubler d'une trace."""
    r = _noeud()
    r.when("--plan", Result(("pct",), 1, "", "20260820 est le dernier instantané"))
    with pytest.raises(Refus):
        Delegate(r, 200).plan("delete", ["20260820"])
    assert "dernier instantané" in capsys.readouterr().err


def test_le_plan_est_transmis_au_moteur_avec_ses_arguments():
    r = _noeud()
    r.when("--plan", Result(("pct",), 0, "x\n", ""))
    Delegate(r, 200).plan("delete", ["20260819"])
    argv = r.calls[0]
    assert argv[:4] == ("pct", "exec", "200", "--")
    assert argv[4] == CT_PGBK
    assert argv[-1] == "--plan"


# ─── délégation ──────────────────────────────────────────────────────────────


def test_hand_over_remplace_le_processus(monkeypatch):
    """Le terminal, l'entrée standard et le code de retour passent sans
    intermédiaire. Une capture par tuyau les perdrait tous les trois."""
    vus = {}
    monkeypatch.setattr("os.execvp", lambda f, a: vus.update(fichier=f, argv=a))
    Delegate(FakeRunner(), 200).hand_over("list", [], yes=False)
    assert vus["fichier"] == "pct"
    assert vus["argv"] == ["pct", "exec", "200", "--", CT_PGBK, "list"]


def test_le_yes_nest_ajoute_quapres_confirmation(monkeypatch):
    vus = {}
    monkeypatch.setattr("os.execvp", lambda f, a: vus.update(argv=a))
    Delegate(FakeRunner(), 200).hand_over("delete", ["20260819-233627"], yes=True)
    assert vus["argv"][-1] == "--yes"


# ─── confirmations ───────────────────────────────────────────────────────────


def test_la_confirmation_exige_la_frappe_exacte():
    """Pas de oui/non : recopier le nom oblige à le lire."""
    confirm("ÉCRASE la base forge", "forge", "le nom de la base", saisie=lambda _: "forge")


def test_une_reponse_approchante_annule():
    with pytest.raises(Refus, match="annulé"):
        confirm("ÉCRASE la base forge", "forge", "le nom", saisie=lambda _: "forg")


def test_un_oui_ne_suffit_pas():
    with pytest.raises(Refus, match="annulé"):
        confirm("ÉCRASE la base forge", "forge", "le nom", saisie=lambda _: "oui")


def test_une_interruption_annule():
    """Ctrl-D ou Ctrl-C au moment de la question : on n'écrase rien."""
    def coupe(_):
        raise EOFError

    with pytest.raises(Refus, match="annulé"):
        confirm("ÉCRASE", "x", "le nom", saisie=coupe)


def test_la_question_dit_ce_quelle_attend():
    vues = []
    confirm("SUPPRIME l'instantané 20260819-233627 du CT 200",
            "20260819-233627", "son nom",
            saisie=lambda q: (vues.append(q), "20260819-233627")[1])
    assert vues[0].endswith("[tapez son nom pour confirmer] : ")
    assert "SUPPRIME l'instantané 20260819-233627 du CT 200" in vues[0]


# ─── divers ──────────────────────────────────────────────────────────────────


def test_first_positional_saute_les_options():
    assert first_positional(["--yes", "forge", "20260819"]) == "forge"
    assert first_positional(["--yes"]) is None


def test_le_refus_du_moteur_est_recopie_verbatim(capsys):
    """Le moteur formate déjà ses lignes. Les repasser par error() en
    ajouterait un second — le journal porterait deux horodatages sur la même
    ligne, ce qui casse le format que tout le reste respecte.

    Constaté en production le 21 août 2026 sur « pg delete latest --plan ».
    """
    ligne = "09:20:34 [ERROR] le dernier instantané est protégé"
    r = _noeud()
    r.when("--plan", Result(("pct",), 1, "", ligne + "\n"))
    with pytest.raises(Refus):
        Delegate(r, 200).plan("delete", ["latest"])
    err = capsys.readouterr().err
    assert err.strip() == ligne
    assert err.count("[ERROR]") == 1, "un seul préfixe, pas deux"
    assert err.count("09:20:34") == 1, "un seul horodatage"


def test_un_refus_muet_najoute_pas_de_ligne_vide(capsys):
    """Un moteur qui échoue sans rien dire ne doit pas produire une ligne
    vide décorée."""
    r = _noeud()
    r.when("--plan", Result(("pct",), 1, "", ""))
    with pytest.raises(Refus):
        Delegate(r, 200).plan("delete", ["x"])
    assert capsys.readouterr().err == ""
