#!/usr/bin/env bash
#
# pg-backup.sh — sauvegarde logique PostgreSQL, un répertoire par exécution.
# Exécuté par pg-backup.timer sous l'utilisateur postgres.
#
# ARBORESCENCE
#   /var/backups/postgresql/
#     20260819-233627/          ← une exécution = un répertoire
#       globals.sql             ← rôles et mots de passe
#       forgejo.dump            ← un dump par base, format -Fc
#       MANIFEST
#     latest -> 20260819-233627
#
# Restaurer, c'est prendre UN répertoire : il contient un point cohérent dans
# le temps. Un dump par base (et non un pg_dumpall monolithique) permet de ne
# restaurer que le service cassé.
#
# ATOMICITÉ — tout est écrit dans <stamp>.part/, renommé en <stamp>/ seulement
# si l'exécution va au bout. Un répertoire présent est donc, par construction,
# une sauvegarde complète. Une exécution interrompue ne laisse rien.
#
# CONTRÔLE D'ESPACE — le script refuse de démarrer s'il ne peut pas garantir
# MIN_FREE_MB libres à l'arrivée. Sur un volume partagé avec PGDATA, saturer
# le disque empêcherait PostgreSQL d'écrire son WAL et l'arrêterait net.
#
# JOURNALISATION — sortie horodatée et préfixée par niveau, lisible aussi bien
# à l'écran que dans journalctl. Diagnostiquer une sauvegarde qui a mal tourné
# trois semaines plus tôt ne doit pas demander de rejouer le script.
#
# --json — émet un objet JSON sur la sortie standard, le journal humain partant
# alors sur la sortie d'erreur. Sert à ce qu'un appelant n'ait JAMAIS à analyser
# des lignes faites pour des humains : une phrase de journal se reformule sans
# prévenir, une clé de JSON non. L'objet est émis DANS TOUS LES CAS, succès
# comme échec — un appelant qui ne reçoit rien ne peut pas distinguer une panne
# d'un script qui n'a pas tourné.
#
# Usage :
#   pg-backup.sh              sauvegarde, journal humain sur la sortie standard
#   pg-backup.sh --json       idem, plus un objet JSON ; journal sur stderr
#   pg-backup.sh --help       cette aide
#
set -Eeuo pipefail

JSON=0
LOGFD=1          # descripteur du journal humain : 2 quand --json occupe stdout

usage() {
  awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json)     JSON=1; LOGFD=2; shift ;;
    -h|--help)  usage ;;
    *)          printf '%s [ERROR] argument inconnu : %s (voir --help)\n' \
                       "$(date '+%H:%M:%S')" "$1" >&2
                exit 1 ;;
  esac
done

DEST="${PG_BACKUP_DEST:-/var/backups/postgresql}"
RETENTION_DAYS="${PG_BACKUP_RETENTION:-14}"
MIN_FREE_MB="${PG_BACKUP_MIN_FREE_MB:-512}"
SIZE_FACTOR="${PG_BACKUP_SIZE_FACTOR:-60}"   # % de la taille brute estimé -Fc
STAMP="$(date +%Y%m%d-%H%M%S)"

WORK="${DEST}/${STAMP}.part"
FINAL="${DEST}/${STAMP}"

# ─── Journalisation ──────────────────────────────────────────────────────────
# Sous systemd, stdout et stderr partent dans le journal. L'horodatage fait
# doublon avec celui de journalctl mais rend le script utilisable à la main.

# En mode --json, stdout est réservé à l'objet : le journal humain bascule sur
# stderr, où journalctl le récupère exactement comme avant.
log()   { printf '%s [INFO ] %s\n'  "$(date '+%H:%M:%S')" "$*" >&$LOGFD; }
warn()  { printf '%s [WARN ] %s\n'  "$(date '+%H:%M:%S')" "$*" >&2; }
error() { printf '%s [ERROR] %s\n'  "$(date '+%H:%M:%S')" "$*" >&2; }
step()  { printf '%s [STEP ] %s\n'  "$(date '+%H:%M:%S')" "$*" >&$LOGFD; }

# ─── Rapport machine ─────────────────────────────────────────────────────────
# Tout ce que --json émettra. Renseigné au fil de l'exécution ; ce qui n'a pas
# eu lieu reste vide, et sort en null plutôt qu'en valeur inventée.

STARTED_AT="$(date -Is)"
PG_VERSION=""
AVAIL_BEFORE=0
NEED_MB=0
TOTAL_MB=0
GLOBALS_BYTES=0
GLOBALS_ROLES=0
GLOBALS_SECONDS=0
PRUNED=0
KEPT=0
FINAL_OK=""
DUMPS_JSON=""
DBS_JSON=""

# Échappement JSON. Les valeurs en jeu sont des chemins, des versions et des
# noms de base : la contre-oblique et le guillemet suffisent, et un chemin
# exotique ne doit pas produire un objet illisible.
jstr() {
  local v=$1
  v=${v//\\/\\\\}
  v=${v//\"/\\\"}
  printf '"%s"' "$v"
}

# Un champ numérique doit TOUJOURS sortir en nombre. Une valeur vide — un psql
# muet, une commande qui a échoué — produirait « "raw_mb":, » et rendrait
# l'objet entier illisible : exactement la panne que --json existe pour
# empêcher. Constaté au banc d'essai le 21 août 2026.
jnum() {
  case "${1:-}" in
    ''|*[!0-9-]*) printf '0' ;;
    *)            printf '%s' "$1" ;;
  esac
}

# PID du shell principal. `set -E` fait hériter le trap ERR aux sous-shells :
# un psql qui échoue dans une substitution de commande y déclencherait
# l'émission du rapport, et l'objet entier serait capturé comme valeur de la
# variable qu'on affectait. Constaté le 21 août 2026 sur un lancement en root :
# le champ « postgresql » contenait un objet JSON complet.
#
# Le rapport n'est donc émis QUE depuis le shell principal ; le trap du parent
# le produira de toute façon, une fois, au bon endroit.
MAIN_PID=$$

emit_json() {
  [[ $JSON -eq 1 ]] || return 0
  [[ $BASHPID == "$MAIN_PID" ]] || return 0
  local statut=$1 code=$2
  local final='null'
  [[ -n $FINAL_OK ]] && final=$(jstr "$FINAL_OK")
  printf '{'
  printf '"schema_version":1,'
  printf '"status":%s,"exit_code":%s,' "$(jstr "$statut")" "$(jnum "$code")"
  printf '"started_at":%s,"finished_at":%s,"duration_s":%s,' \
         "$(jstr "$STARTED_AT")" "$(jstr "$(date -Is)")" "$(jnum "$SECONDS")"
  printf '"host":%s,"postgresql":%s,' \
         "$(jstr "$(hostname)")" "$(jstr "$PG_VERSION")"
  printf '"dest":%s,"stamp":%s,"final_dir":%s,' \
         "$(jstr "$DEST")" "$(jstr "$STAMP")" "$final"
  printf '"config":{"retention_days":%s,"min_free_mb":%s,"size_factor":%s},' \
         "$(jnum "$RETENTION_DAYS")" "$(jnum "$MIN_FREE_MB")" "$(jnum "$SIZE_FACTOR")"
  printf '"space":{"avail_mb_before":%s,"need_mb":%s,"total_raw_mb":%s,"avail_mb_after":%s},' \
         "$(jnum "$AVAIL_BEFORE")" "$(jnum "$NEED_MB")" "$(jnum "$TOTAL_MB")" "$(jnum "$(avail_mb)")"
  printf '"globals":{"bytes":%s,"roles":%s,"duration_s":%s},' \
         "$(jnum "$GLOBALS_BYTES")" "$(jnum "$GLOBALS_ROLES")" "$(jnum "$GLOBALS_SECONDS")"
  printf '"databases":[%s],' "$DBS_JSON"
  printf '"dumps":[%s],' "$DUMPS_JSON"
  printf '"pruned":%s,"kept":%s' "$(jnum "$PRUNED")" "$(jnum "$KEPT")"
  printf '}\n'
}

on_error() {
  local rc=$? line=$1
  error "échec ligne ${line} (code ${rc}) — après ${SECONDS}s"
  if [[ -d $WORK ]]; then
    warn "suppression du répertoire incomplet ${WORK}"
    rm -rf "$WORK"
  fi
  error "AUCUNE sauvegarde produite pour cette exécution"
  emit_json error "$rc"
  exit "$rc"
}
trap 'on_error $LINENO' ERR
trap 'error "interrompu par signal"; [[ -d $WORK ]] && rm -rf "$WORK"; emit_json interrupted 130; exit 130' INT TERM

umask 077
mkdir -p "$DEST"

avail_mb()   { df -Pm "$DEST" | awk 'NR==2 {print $4}'; }
# Taille logique, lisible. `du` donnerait l'occupation réelle sur disque, qui
# est plus petite sur ZFS (compression lz4) et prête à confusion dans un
# journal : on veut savoir ce que pèse le dump, pas ce que le pool en fait.
hsize()      { numfmt --to=iec --suffix=B "$(stat -c%s "$1")"; }
dsize()      { du -sh --apparent-size "$1" | cut -f1; }
db_size_mb() { psql -tAc "SELECT ceil(pg_database_size('$1')/1024.0/1024)"; }

prune() {
  local n=0 d
  while IFS= read -r d; do
    log "  purge (expiré > ${RETENTION_DAYS} j) : $(basename "$d") — $(du -sh --apparent-size "$d" | cut -f1)"
    rm -rf "$d"; n=$((n+1))
  done < <(find "$DEST" -mindepth 1 -maxdepth 1 -type d -name '20*' \
                ! -name '*.part' -mtime "+${RETENTION_DAYS}")

  while IFS= read -r d; do
    warn "  débris d'une exécution tuée brutalement : $(basename "$d")"
    rm -rf "$d"; n=$((n+1))
  done < <(find "$DEST" -mindepth 1 -maxdepth 1 -type d -name '*.part' -mtime +1)

  PRUNED=$(( PRUNED + n ))
  [[ $n -eq 0 ]] && log "  rien à purger" || log "  ${n} répertoire(s) supprimé(s)"
}

# ─── Démarrage ───────────────────────────────────────────────────────────────
step "démarrage — destination ${DEST}"
log "  rétention ${RETENTION_DAYS} j | marge minimale ${MIN_FREE_MB} Mo | facteur de compression ${SIZE_FACTOR} %"
PG_VERSION="$(psql -tAc 'SHOW server_version')"
AVAIL_BEFORE=$(avail_mb)
log "  PostgreSQL $(printf '%s' "$PG_VERSION" | awk '{print $1}') sur $(hostname)"
log "  espace disponible : ${AVAIL_BEFORE} Mo"

# ─── Inventaire ──────────────────────────────────────────────────────────────
step "inventaire des bases"
mapfile -t DBS < <(psql -tAc \
  "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate AND datname <> 'postgres'")

if [[ ${#DBS[@]} -eq 0 ]]; then
  warn "aucune base à sauvegarder — le cluster ne contient que les templates"
  emit_json no_databases 0
  exit 0
fi

for db in "${DBS[@]}"; do
  DBS_JSON+="${DBS_JSON:+,}$(jstr "$db")"
done

TOTAL_MB=0
for db in "${DBS[@]}"; do
  s=$(db_size_mb "$db")
  log "  ${db} : ${s} Mo"
  TOTAL_MB=$(( TOTAL_MB + s ))
done
NEED_MB=$(( TOTAL_MB * SIZE_FACTOR / 100 + MIN_FREE_MB ))
log "  ${#DBS[@]} base(s), ${TOTAL_MB} Mo bruts — besoin estimé ${NEED_MB} Mo"

# ─── Contrôle d'espace, AVANT d'écrire quoi que ce soit ──────────────────────
step "contrôle d'espace"
if [[ $(avail_mb) -lt $NEED_MB ]]; then
  warn "  $(avail_mb) Mo libres pour ${NEED_MB} Mo requis — purge anticipée des expirés"
  prune
fi

if [[ $(avail_mb) -lt $NEED_MB ]]; then
  error "espace insuffisant : $(avail_mb) Mo libres, ${NEED_MB} Mo requis sur ${DEST}"
  error "aucune sauvegarde effectuée — libérer de l'espace ou agrandir le volume"
  error "  pct resize <CTID> mp2 +20G   (depuis l'hôte Proxmox)"
  emit_json insufficient_space 1
  exit 1
fi
log "  OK — $(avail_mb) Mo libres pour ${NEED_MB} Mo requis"

# ─── Exécution ───────────────────────────────────────────────────────────────
mkdir -p "$WORK"
log "répertoire de travail : ${WORK}"

step "globals (rôles et mots de passe)"
# Les rôles et leurs mots de passe ne sont dans AUCUN pg_dump de base. Sans ce
# fichier, une restauration rend les données sans les comptes qui y accèdent.
# Contient des empreintes SCRAM : fichier le plus sensible du lot.
t0=$SECONDS
pg_dumpall --globals-only > "${WORK}/globals.sql"
GLOBALS_BYTES=$(stat -c%s "${WORK}/globals.sql")
GLOBALS_ROLES=$(grep -c '^CREATE ROLE' "${WORK}/globals.sql" || true)
GLOBALS_SECONDS=$((SECONDS-t0))
log "  globals.sql — $(hsize "${WORK}/globals.sql"), ${GLOBALS_ROLES} rôle(s), ${GLOBALS_SECONDS}s"

step "dumps par base"
for db in "${DBS[@]}"; do
  raw=$(db_size_mb "$db")
  need=$(( raw * SIZE_FACTOR / 100 + MIN_FREE_MB ))
  if [[ $(avail_mb) -lt $need ]]; then
    error "plus assez d'espace pour ${db} : $(avail_mb) Mo libres, ${need} Mo requis"
    emit_json insufficient_space 1
    exit 1
  fi

  t0=$SECONDS
  pg_dump -Fc --no-owner --no-acl "$db" > "${WORK}/${db}.dump"
  DUMPS_JSON+="${DUMPS_JSON:+,}{\"database\":$(jstr "$db"),\"raw_mb\":$(jnum "$raw"),"
  DUMPS_JSON+="\"bytes\":$(jnum "$(stat -c%s "${WORK}/${db}.dump")"),\"duration_s\":$(jnum "$((SECONDS-t0))")}"
  log "  ${db} — $(hsize "${WORK}/${db}.dump") depuis ${raw} Mo bruts, $((SECONDS-t0))s"
done

{
  echo "date        : $(date -Is)"
  echo "postgresql  : $(psql -tAc 'SHOW server_version')"
  echo "hôte        : $(hostname)"
  echo "bases       : ${DBS[*]}"
} > "${WORK}/MANIFEST"

# ─── Bascule ─────────────────────────────────────────────────────────────────
# Le renommage est le point de non-retour : à partir d'ici, la sauvegarde existe.
mv "$WORK" "$FINAL"
ln -sfn "$FINAL" "${DEST}/latest"
trap - ERR INT TERM
FINAL_OK="$FINAL"
step "sauvegarde validée : ${FINAL}"

step "rétention"
prune

# ─── Résumé ──────────────────────────────────────────────────────────────────
# ! -name '*.part' : un débris de moins de 48 h n'est pas une sauvegarde
# conservée, et le compter en gonflerait le bilan.
KEPT=$(find "$DEST" -mindepth 1 -maxdepth 1 -type d -name '20*' ! -name '*.part' | wc -l)
step "terminé en ${SECONDS}s — ${#DBS[@]} base(s), $(dsize "$FINAL") produits"
log "  ${KEPT} sauvegarde(s) conservée(s), $(dsize "$DEST") logiques / $(du -sh "$DEST" | cut -f1) sur disque"
log "  espace restant : $(avail_mb) Mo"

emit_json ok 0