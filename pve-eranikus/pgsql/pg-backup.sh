#!/usr/bin/env bash
#
# pg-backup.sh — dump par base + globals, avec rétention.
# Exécuté par pg-backup.timer sous l'utilisateur postgres.
#
# Un dump PAR BASE (et non un pg_dumpall monolithique) : le jour où tu ne
# casses que Forgejo, tu ne restaures que Forgejo.
#
set -Eeuo pipefail

DEST="${PG_BACKUP_DEST:-/var/backups/postgresql}"
RETENTION_DAYS="${PG_BACKUP_RETENTION:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"

umask 077
mkdir -p "$DEST"

# Les rôles et leurs mots de passe ne sont dans AUCUN pg_dump de base.
# Sans ce fichier, une restauration te rend les données sans les comptes.
pg_dumpall --globals-only > "${DEST}/globals-${STAMP}.sql.part"

# Liste des bases réelles, hors templates et hors postgres.
mapfile -t DBS < <(psql -tAc \
  "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate AND datname <> 'postgres'")

for db in "${DBS[@]}"; do
  # -Fc : format custom, compressé, restaurable table par table avec pg_restore.
  pg_dump -Fc --no-owner --no-acl "$db" > "${DEST}/${db}-${STAMP}.dump"
  echo "dump: ${db} ($(du -h "${DEST}/${db}-${STAMP}.dump" | cut -f1))"
done

# Rétention locale. La copie hors-site (rclone vers GCS) se fait ensuite,
# depuis l'hôte Proxmox ou via une seconde unité.
find "$DEST" -type f \( -name '*.dump' -o -name 'globals-*.sql' \) \
     -mtime "+${RETENTION_DAYS}" -delete

echo "sauvegarde terminée : ${#DBS[@]} base(s) dans ${DEST}"
