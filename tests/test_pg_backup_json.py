"""`pg-backup.sh --json` — le seul script qui reste en bash, testé pour de vrai.

Il tourne ici tel quel, avec `psql`, `pg_dumpall` et `pg_dump` bouchonnés ; le
reste (`df`, `du`, `stat`, `find`, `numfmt`) est le vrai. C'est la seule façon
honnête de vérifier un script dont on a décidé qu'il ne serait PAS porté :
le lire ne suffit pas, et l'exécuter sur l'infrastructure n'est pas une option.

Pourquoi `--json` : pour que le côté Python n'ait jamais à analyser une sortie
faite pour des humains. Une ligne de journal se reformule sans prévenir ; une
clé de JSON, non.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "pve-eranikus" / "pgsql" / "ct" / "pg-backup.sh"

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="pg-backup.sh absent")

VERSION_PG = "18.6 (Debian 18.6-1.pgdg13+2)"


def _stub(dossier: Path, nom: str, corps: str) -> None:
    chemin = dossier / nom
    chemin.write_text("#!/bin/bash\n" + corps)
    chemin.chmod(chemin.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def cluster(tmp_path):
    """Un cluster bouchonné : une base « forgejo » de 12 Mo."""
    binaires = tmp_path / "bin"
    binaires.mkdir()

    _stub(binaires, "psql", f"""
case "$*" in
  *"SHOW server_version"*) echo "{VERSION_PG}" ;;
  *"FROM pg_database"*)    echo "forgejo" ;;
  *pg_database_size*)      echo 12 ;;
  *)                       echo "" ;;
esac
""")
    _stub(binaires, "pg_dumpall", """
echo "CREATE ROLE forgejo;"
echo "CREATE ROLE jbwittner;"
""")
    # 2100 octets, pour une taille lisible et stable.
    _stub(binaires, "pg_dump", 'printf "%02100d" 0')

    dest = tmp_path / "postgresql"
    dest.mkdir()
    return {"bin": binaires, "dest": dest}


def _lancer(cluster, *args, attendu=0):
    env = dict(os.environ)
    env["PATH"] = f"{cluster['bin']}:{env['PATH']}"
    env["PG_BACKUP_DEST"] = str(cluster["dest"])
    res = subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == attendu, res.stdout + res.stderr
    return res


# ─── le comportement par défaut ne change pas ────────────────────────────────


def test_sans_json_la_sortie_reste_celle_dhier(cluster):
    """Le contrat le plus important : ajouter une option ne doit rien changer
    à ce que fait le script quand on ne la donne pas."""
    res = _lancer(cluster)
    assert "[STEP ] démarrage" in res.stdout
    assert "sauvegarde validée" in res.stdout
    assert not res.stdout.lstrip().startswith("{"), "aucun JSON sans --json"


def test_sans_json_la_sauvegarde_est_bien_produite(cluster):
    _lancer(cluster)
    # `latest` est un lien vers un répertoire : `is_dir()` le suit, et il
    # serait compté comme un instantané de plus.
    instantanes = [p for p in cluster["dest"].iterdir()
                   if p.is_dir() and not p.is_symlink()
                   and not p.name.endswith(".part")]
    assert len(instantanes) == 1
    fichiers = {p.name for p in instantanes[0].iterdir()}
    assert fichiers == {"globals.sql", "forgejo.dump", "MANIFEST"}
    assert (cluster["dest"] / "latest").is_symlink()


# ─── --json ──────────────────────────────────────────────────────────────────


def test_json_seul_sur_la_sortie_standard(cluster):
    """stdout ne porte QUE le JSON ; le journal humain part sur stderr, où
    journalctl le récupère comme avant."""
    res = _lancer(cluster, "--json")
    donnees = json.loads(res.stdout)
    assert isinstance(donnees, dict)
    assert "[STEP ] démarrage" in res.stderr


def test_json_dit_ce_qui_a_ete_produit(cluster):
    donnees = json.loads(_lancer(cluster, "--json").stdout)
    assert donnees["status"] == "ok"
    assert donnees["exit_code"] == 0
    assert donnees["databases"] == ["forgejo"]
    assert donnees["stamp"]
    assert donnees["final_dir"].endswith(donnees["stamp"])
    assert donnees["postgresql"] == VERSION_PG


def test_json_porte_le_parametrage_effectif(cluster):
    """Les valeurs réelles viennent de l'unité systemd : les relire dans le
    JSON évite de les redeviner ailleurs."""
    donnees = json.loads(_lancer(cluster, "--json").stdout)
    assert donnees["config"]["retention_days"] == 14
    assert donnees["config"]["min_free_mb"] == 512
    assert donnees["config"]["size_factor"] == 60


def test_json_donne_les_tailles_en_octets(cluster):
    """Des octets, pas des « 2.1K » : un consommateur qui compare des tailles
    n'a pas à défaire un arrondi d'affichage."""
    donnees = json.loads(_lancer(cluster, "--json").stdout)
    assert donnees["globals"]["bytes"] > 0
    assert donnees["globals"]["roles"] == 2
    dump = donnees["dumps"][0]
    assert dump["database"] == "forgejo"
    assert dump["bytes"] == 2100
    assert dump["raw_mb"] == 12


def test_json_est_valide_meme_sur_un_cluster_vide(cluster):
    """Aucune base à sauvegarder est un succès, pas une panne — mais le JSON
    doit exister quand même, sinon l'appelant ne sait rien."""
    # `psql -tAc` n'émet RIEN sur un résultat vide — pas même une ligne vide.
    # Un « echo "" » produirait un élément vide dans mapfile, et le script
    # croirait avoir une base nommée « ».
    _stub(cluster["bin"], "psql", """
case "$*" in
  *"SHOW server_version"*) echo "18.6" ;;
  *"FROM pg_database"*)    printf '' ;;
  *)                       printf '' ;;
esac
""")
    donnees = json.loads(_lancer(cluster, "--json").stdout)
    assert donnees["status"] == "no_databases"
    assert donnees["exit_code"] == 0
    assert donnees["databases"] == []


def test_json_est_emis_meme_en_cas_dechec(cluster):
    """C'est le cas qui compte : un appelant qui ne reçoit rien ne peut pas
    distinguer un échec d'un script qui n'a pas tourné."""
    _stub(cluster["bin"], "pg_dump", 'echo "boum" >&2; exit 1')
    res = _lancer(cluster, "--json", attendu=1)
    donnees = json.loads(res.stdout)
    assert donnees["status"] == "error"
    assert donnees["exit_code"] == 1
    assert donnees["final_dir"] is None, "aucune sauvegarde produite"


def test_un_echec_ne_laisse_pas_de_sauvegarde_incomplete(cluster):
    _stub(cluster["bin"], "pg_dump", 'echo "boum" >&2; exit 1')
    _lancer(cluster, "--json", attendu=1)
    restants = [p.name for p in cluster["dest"].iterdir()]
    assert restants == [], "le répertoire de travail doit être nettoyé"


def test_un_champ_numerique_vide_ne_casse_pas_lobjet(cluster):
    """Un psql muet au mauvais moment donnait « "raw_mb":, », donc un objet
    entier illisible — exactement la panne que --json existe pour empêcher.

    Constaté au banc d'essai le 21 août 2026 : un bouchon qui renvoyait une
    ligne vide faisait croire au script qu'il existait une base sans nom.
    """
    _stub(cluster["bin"], "psql", """
case "$*" in
  *"SHOW server_version"*) echo "18.6" ;;
  *"FROM pg_database"*)    echo "forgejo" ;;
  *pg_database_size*)      printf '' ;;
  *)                       printf '' ;;
esac
""")
    res = _lancer(cluster, "--json", attendu=0)
    donnees = json.loads(res.stdout)
    assert donnees["dumps"][0]["raw_mb"] == 0


def test_un_argument_inconnu_est_refuse(cluster):
    res = _lancer(cluster, "--inconnu", attendu=1)
    assert "inconnu" in res.stderr


def test_laide_ne_sauvegarde_rien(cluster):
    res = _lancer(cluster, "--help")
    assert "pg-backup.sh" in res.stdout
    assert list(cluster["dest"].iterdir()) == []


# ─── échappement ─────────────────────────────────────────────────────────────


def test_le_json_reste_valide_sur_un_chemin_exotique(tmp_path):
    """Un chemin avec un guillemet ou une contre-oblique casserait un JSON
    construit par concaténation naïve."""
    binaires = tmp_path / "bin"
    binaires.mkdir()
    _stub(binaires, "psql", """
case "$*" in
  *"SHOW server_version"*) echo '18.6 "trixie" \\\\ test' ;;
  *"FROM pg_database"*)    echo "forgejo" ;;
  *pg_database_size*)      echo 1 ;;
  *)                       echo "" ;;
esac
""")
    _stub(binaires, "pg_dumpall", 'echo "CREATE ROLE x;"')
    _stub(binaires, "pg_dump", 'printf "x"')
    dest = tmp_path / "postgresql"
    dest.mkdir()

    donnees = json.loads(_lancer(
        {"bin": binaires, "dest": dest}, "--json"
    ).stdout)
    assert '"' in donnees["postgresql"] and "\\" in donnees["postgresql"]
