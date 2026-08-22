#!/usr/bin/env python3
"""fj-check.py — santé de la pile Forgejo. Une ligne par contrôle, 0 ou 1.

Appelé EN FIN DE `fjbk backup`, et c'est là qu'il gagne sa place : la sauvegarde
arrête Forgejo, donc la seule question qui compte ensuite est « est-il bien
remonté ? ». Un job vert qui laisse le service à terre serait le pire des deux
mondes. Son code de retour devient le 3 de `fjbk` — intervention humaine.

`--json` pour un usage futur par Homepage : une phrase de journal se reformule
sans prévenir, une clé de JSON non.
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

COMPOSE = Path(__file__).resolve().parent.parent / "compose.yaml"
BACKUPS = Path("/srv/forgejo/backups")
SRV = Path("/srv")
URL = "http://127.0.0.1:3000/api/healthz"
AGE_MAX_H = 48
LIBRE_MIN_MO = 4096
CONTENEURS = ("forgejo", "forgejo-db")


def run(argv):
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def conteneurs():
    """`docker inspect` plutôt que `docker compose ps` : l'état demandé est un
    mot exact, pas une colonne d'un tableau fait pour des humains."""
    absents = []
    for nom in CONTENEURS:
        r = run(["docker", "inspect", "-f", "{{.State.Status}}", nom])
        etat = r.stdout.strip() if r.returncode == 0 else "absent"
        if etat != "running":
            absents.append(f"{nom}={etat}")
    return (not absents, "les deux conteneurs tournent" if not absents
            else "conteneur(s) hors service : " + ", ".join(absents))


def api():
    try:
        with urllib.request.urlopen(URL, timeout=10) as reponse:
            corps = json.loads(reponse.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{URL} ne répond pas : {exc}"
    statut = corps.get("status", "?")
    return statut == "pass", f"{URL} répond « {statut} »"


def base():
    r = run(["docker", "compose", "-f", str(COMPOSE), "exec", "-T", "db",
             "pg_isready", "-U", "forgejo", "-d", "forgejo"])
    return r.returncode == 0, (r.stdout or r.stderr).strip() or "pg_isready muet"


def sauvegarde():
    """La PAIRE, pas un fichier : un dump sans son archive ne restaure rien.
    L'âge se lit sur le mtime, pas sur le nom — une paire rapatriée de GCS porte
    un nom ancien et un mtime récent."""
    if not BACKUPS.is_dir():
        return False, f"{BACKUPS} n'existe pas"
    stamps = {}
    for f in BACKUPS.iterdir():
        if f.name.startswith("db-") and f.name.endswith(".dump"):
            stamps.setdefault(f.name[3:-5], {})["db"] = f
        elif f.name.startswith("data-") and f.name.endswith(".tar.gz"):
            stamps.setdefault(f.name[5:-7], {})["data"] = f
    completes = {s: p for s, p in stamps.items() if len(p) == 2}
    if not completes:
        return False, f"aucune paire complète dans {BACKUPS}"
    dernier = max(completes)
    age = int((time.time() - min(f.stat().st_mtime
                                 for f in completes[dernier].values())) // 3600)
    return age <= AGE_MAX_H, f"dernière paire {dernier}, {age} h (seuil {AGE_MAX_H} h)"


def espace():
    libre = shutil.disk_usage(SRV).free // 1048576
    return libre >= LIBRE_MIN_MO, f"{libre} Mio libres sur {SRV} (plancher {LIBRE_MIN_MO})"


CONTROLES = (
    ("conteneurs", conteneurs),
    ("api", api),
    ("base", base),
    ("sauvegarde", sauvegarde),
    ("espace", espace),
)


def main():
    p = argparse.ArgumentParser(prog="fj-check.py", description=__doc__.splitlines()[0])
    p.add_argument("--json", action="store_true", help="un objet par contrôle")
    args = p.parse_args()

    resultats = {}
    for nom, controle in CONTROLES:
        try:
            ok, detail = controle()
        except Exception as exc:  # un contrôle qui explose est un contrôle rouge
            ok, detail = False, f"contrôle en erreur : {exc}"
        resultats[nom] = {"ok": ok, "detail": detail}

    verdict = all(r["ok"] for r in resultats.values())
    if args.json:
        print(json.dumps({"ok": verdict, "controles": resultats}, ensure_ascii=False))
    else:
        for nom, r in resultats.items():
            print(f"  [{'OK ' if r['ok'] else 'KO '}] {nom:<12} {r['detail']}")
        if not verdict:
            print("verdict : la pile demande une intervention — voir doc/PRA.md",
                  file=sys.stderr)
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
