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
set -Eeuo pipefail

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

log()   { printf '%s [INFO ] %s\n'  "$(date '+%H:%M:%S')" "$*"; }
warn()  { printf '%s [WARN ] %s\n'  "$(date '+%H:%M:%S')" "$*" >&2; }
error() { printf '%s [ERROR] %s\n'  "$(date '+%H:%M:%S')" "$*" >&2; }
step()  { printf '%s [STEP ] %s\n'  "$(date '+%H:%M:%S')" "$*"; }

on_error() {
  local rc=$? line=$1
  error "échec ligne ${line} (code ${rc}) — après ${SECONDS}s"
  if [[ -d $WORK ]]; then
    warn "suppression du répertoire incomplet ${WORK}"
    rm -rf "$WORK"
  fi
  error "AUCUNE sauvegarde produite pour cette exécution"
  exit "$rc"
}
trap 'on_error $LINENO' ERR
trap 'error "interrompu par signal"; [[ -d $WORK ]] && rm -rf "$WORK"; exit 130' INT TERM

umask 077
mkdir -p "$DEST"

avail_mb()   { df -Pm "$DEST" | awk 'NR==2 {print $4}'; }
db_size_mb() { psql -tAc "SELECT ceil(pg_database_size('$1')/1024.0/1024)"; }

prune() {
  local n=0 d
  while IFS= read -r d; do
    log "  purge (expiré > ${RETENTION_DAYS} j) : $(basename "$d") — $(du -sh "$d" | cut -f1)"
    rm -rf "$d"; n=$((n+1))
  done < <(find "$DEST" -mindepth 1 -maxdepth 1 -type d -name '20*' \
                ! -name '*.part' -mtime "+${RETENTION_DAYS}")

  while IFS= read -r d; do
    warn "  débris d'une exécution tuée brutalement : $(basename "$d")"
    rm -rf "$d"; n=$((n+1))
  done < <(find "$DEST" -mindepth 1 -maxdepth 1 -type d -name '*.part' -mtime +1)

  [[ $n -eq 0 ]] && log "  rien à purger" || log "  ${n} répertoire(s) supprimé(s)"
}

# ─── Démarrage ───────────────────────────────────────────────────────────────
step "démarrage — destination ${DEST}"
log "  rétention ${RETENTION_DAYS} j | marge minimale ${MIN_FREE_MB} Mo | facteur de compression ${SIZE_FACTOR} %"
log "  PostgreSQL $(psql -tAc 'SHOW server_version') sur $(hostname)"
log "  espace disponible : $(avail_mb) Mo"

# ─── Inventaire ──────────────────────────────────────────────────────────────
step "inventaire des bases"
mapfile -t DBS < <(psql -tAc \
  "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate AND datname <> 'postgres'")

if [[ ${#DBS[@]} -eq 0 ]]; then
  warn "aucune base à sauvegarder — le cluster ne contient que les templates"
  exit 0
fi

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
log "  globals.sql — $(du -h "${WORK}/globals.sql" | cut -f1), $(grep -c '^CREATE ROLE' "${WORK}/globals.sql" || true) rôle(s), $((SECONDS-t0))s"

step "dumps par base"
for db in "${DBS[@]}"; do
  raw=$(db_size_mb "$db")
  need=$(( raw * SIZE_FACTOR / 100 + MIN_FREE_MB ))
  if [[ $(avail_mb) -lt $need ]]; then
    error "plus assez d'espace pour ${db} : $(avail_mb) Mo libres, ${need} Mo requis"
    exit 1
  fi

  t0=$SECONDS
  pg_dump -Fc --no-owner --no-acl "$db" > "${WORK}/${db}.dump"
  sz=$(du -h "${WORK}/${db}.dump" | cut -f1)
  szb=$(( $(stat -c%s "${WORK}/${db}.dump") / 1024 ))
  log "  ${db} — ${sz} (${raw} Mo bruts), $((SECONDS-t0))s, ${szb} Kio écrits"
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
step "sauvegarde validée : ${FINAL}"

step "rétention"
prune

# ─── Résumé ──────────────────────────────────────────────────────────────────
KEPT=$(find "$DEST" -mindepth 1 -maxdepth 1 -type d -name '20*' | wc -l)
step "terminé en ${SECONDS}s — ${#DBS[@]} base(s), $(du -sh "$FINAL" | cut -f1) écrits"
log "  ${KEPT} sauvegarde(s) conservée(s), $(du -sh "$DEST" | cut -f1) au total"
log "  espace restant : $(avail_mb) Mo"