"""Les accords entre fichiers, que rien d'autre ne vérifie.

Un chemin écrit à deux endroits finit par diverger, et la panne se découvre à
3h30 dans le journal. Ces tests relient le lanceur, le déployeur et l'unité
systemd : ils tombent le jour où l'un des trois bouge seul.

Ils s'ancraient sur `pg-deploy.sh`. Celui-ci retiré, ils se sont mis à se
sauter en silence — et un test sauté ne protège rien. Ils sont donc réancrés
sur ce qui fait foi désormais : `pgtool.deploy`, le lanceur, et l'unité.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVICE = REPO / "pve-eranikus" / "pgsql"
LANCEUR = SERVICE / "pg"
UNITE = SERVICE / "host" / "pgbk-offsite.service"


def test_le_lanceur_et_le_deployeur_saccordent_sur_larbre_dimport():
    """`pg` cherche ses paquets là où le déploiement les pose. Si les deux
    divergent, `pg` ne démarre plus et rien ne l'annonce avant la première
    exécution du timer."""
    from pgtool.deploy import HOST_LIB

    assert f'"{HOST_LIB}"' in LANCEUR.read_text(encoding="utf-8")


def test_lunite_appelle_le_lanceur_la_ou_il_est_pose():
    """ExecStart doit désigner exactement le chemin d'installation, en absolu :
    le PATH de systemd n'inclut ni /usr/local/bin ni /usr/local/sbin."""
    from pgtool.deploy import HOST_PG

    lignes = [
        l for l in UNITE.read_text(encoding="utf-8").splitlines()
        if l.startswith("ExecStart=")
    ]
    assert lignes == [f"ExecStart={HOST_PG} offsite"]
    assert str(HOST_PG).startswith("/"), "chemin absolu obligatoire"


def test_lunite_garde_son_nom():
    """Le renommer orphelinerait le drop-in 10-noeud.conf, le lien
    d'activation et tout l'historique du journal."""
    assert UNITE.name == "pgbk-offsite.service"
    assert (UNITE.parent / "pgbk-offsite.timer").exists()


def test_les_lignes_environment_restent_analysables():
    """`unit_env` les relit : une par ligne, sans guillemets. C'est ce qui fait
    de l'unité la source de vérité des chemins hors-site."""
    for ligne in UNITE.read_text(encoding="utf-8").splitlines():
        if not ligne.startswith("Environment="):
            continue
        corps = ligne[len("Environment="):]
        assert corps.count("=") == 1, f"valeur ambiguë : {ligne}"
        assert '"' not in corps and "'" not in corps, f"guillemets : {ligne}"


def test_le_deploiement_lit_bien_les_valeurs_de_lunite():
    """L'accord ne tient pas parce que le format est lisible : il tient parce
    que le lecteur en tire les bonnes valeurs. On le vérifie sur l'unité
    réelle, pas sur un exemple."""
    from pgtool.steps.horssite import unit_env

    assert unit_env(UNITE, "PGBK_OFFSITE_RCLONE", "") == "/usr/bin/rclone"
    assert unit_env(UNITE, "PGBK_OFFSITE_KEY", "").endswith(".json")
    assert unit_env(UNITE, "PGBK_OFFSITE_REMOTE", "") != ""


def test_larbre_dimport_du_noeud_porte_les_trois_paquets():
    """Refuser un dépôt incomplet plutôt que poser un arbre d'import à trous :
    un module manquant ne se découvre qu'à l'import, donc au pire moment."""
    from pgtool.steps.hote import PgtoolHote

    class FauxCtx:
        class paths:
            lib_src = REPO / "lib"
            pgtool_src = SERVICE / "pgtool"

    trouves = PgtoolHote()._sources(FauxCtx())
    for attendu in ("core/__init__.py", "proxmox/__init__.py", "pgtool/cli.py"):
        assert attendu in trouves, f"absent de l'arbre d'import : {attendu}"


def test_le_conteneur_ne_recoit_que_deux_paquets_sur_trois():
    """Le CT n'a pas `pct`. Lui pousser `proxmox` laisserait passer un import
    qui n'échouerait que de l'autre côté du montage."""
    from pgtool.steps.conteneur import MoteurCT

    class FauxCtx:
        class paths:
            lib_src = REPO / "lib"
            pgtool_src = SERVICE / "pgtool"

    trouves = MoteurCT()._sources(FauxCtx())
    assert "core/__init__.py" in trouves
    assert "pgtool/cli.py" in trouves
    assert not any(rel.startswith("proxmox/") for rel in trouves)


def test_le_lanceur_controle_la_version_de_python():
    """Un seuil dans le lanceur et un autre ailleurs donneraient un
    déploiement vert et un `pg` qui refuse de tourner. Il n'y en a donc plus
    qu'un, dans `core`, et le lanceur l'appelle."""
    source = LANCEUR.read_text(encoding="utf-8")
    assert "require_python" in source
    assert "from core import require_python" in source


# ─── ce qui a été retiré ─────────────────────────────────────────────────────


def test_les_scripts_remplaces_ont_quitte_le_depot():
    """La bascule est franche. Les garder « au cas où » garantit que quelqu'un
    les rejouera dans un an, et ils divergeraient sans que rien ne le dise."""
    assert not (SERVICE / "pg-deploy.sh").exists()
    assert not (SERVICE / "host" / "pgbk-offsite.sh").exists()


def test_le_deploiement_RETIRE_le_binaire_hors_site_du_noeud():
    """Supprimer un script du dépôt ne le retire pas du nœud : le binaire
    installé y reste, exécutable et périmé. C'est le seul scénario que la
    section H existe pour empêcher."""
    from core.converge import Mode
    from core.runner import FakeRunner
    from pgtool.deploy import Options, Paths, contexte
    from pgtool.plan import SCRIPT_HORSSITE, etapes

    ctx = contexte(
        runner=FakeRunner(), paths=Paths(src=SERVICE),
        opts=Options(ctid=200), mode=Mode.STATUS,
    )
    retraits = [e for e in etapes(ctx) if getattr(e, "section", "") == "H"]
    assert retraits, "aucune étape de retrait dans le plan"
    vises = {str(e.chemin) for e in retraits}
    assert str(SCRIPT_HORSSITE) in vises


def test_le_moteur_bash_du_CONTENEUR_est_conserve():
    """Il n'est PAS retiré, et c'est délibéré : la restauration est un chemin
    de secours, et l'exercice de bascule qui prouverait le moteur Python n'a
    pas été joué. Ce test tombera le jour où quelqu'un le retire par
    inadvertance."""
    assert (SERVICE / "ct" / "pgbk.sh").exists()
