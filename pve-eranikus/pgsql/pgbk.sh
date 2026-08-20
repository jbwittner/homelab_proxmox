#!/usr/bin/env bash
#
# pgbk — gestion des sauvegardes PostgreSQL du cluster mutualisé.
#
#   pgbk backup                     lance une sauvegarde
#   pgbk list                       liste les sauvegardes disponibles
#   pgbk show [instantané]          détail d'une sauvegarde
#   pgbk restore <base> [instantané]  restaure une base
#   pgbk verify <base>              contrôle l'état d'une base restaurée
#
# L'instantané se désigne par :
#   latest            la plus récente (défaut)
#   20260820-093240   horodatage exact
#   20260820          la plus récente de ce jour
#
# pg-backup.sh reste le moteur, appelé par le timer. pgbk est l'interface
# humaine : il n'écrit aucune sauvegarde lui-même.
#
set -Eeuo pipefail

DEST="${PG_BACKUP_DEST:-/var/backups/postgresql}"
PSQL="sudo -u postgres psql"
ASSUME_YES=0

log()   { printf '%s [INFO ] %s\n'  "$(date '+%H:%M:%S')" "$*"; }
warn()  { printf '%s [WARN ] %s\n'  "$(date '+%H:%M:%S')" "$*" >&2; }
error() { printf '%s [ERROR] %s\n'  "$(date '+%H:%M:%S')" "$*" >&2; }
step()  { printf '%s [STEP ] %s\n'  "$(date '+%H:%M:%S')" "$*"; }
die()   { error "$*"; exit 1; }

usage() { sed -n '3,20p' "$0" | sed 's/^# \?//'; exit "${1:-0}"; }

[[ $EUID -eq 0 ]] || die "à lancer en root dans le CT (pct enter 200)"

# ─── Résolution d'un instantané ──────────────────────────────────────────────

resolve() {
  local ref="${1:-latest}" path
  case "$ref" in
    latest)
      [[ -L "${DEST}/latest" ]] || die "aucune sauvegarde : ${DEST}/latest absent"
      path="$(readlink -f "${DEST}/latest")"
      ;;
    [0-9]*-[0-9]*)
      path="${DEST}/${ref}"
      ;;
    [0-9]*)
      # Un jour : on prend la plus récente de cette date.
      path="$(find "$DEST" -mindepth 1 -maxdepth 1 -type d -name "${ref}-*" \
              ! -name '*.part' | sort | tail -1)"
      [[ -n $path ]] || die "aucune sauvegarde pour le ${ref}"
      ;;
    *)
      die "référence incomprise : ${ref} (attendu: latest, AAAAMMJJ, ou AAAAMMJJ-HHMMSS)"
      ;;
  esac
  [[ -d $path ]] || die "instantané introuvable : ${path}"
  echo "$path"
}

snapshots() {
  find "$DEST" -mindepth 1 -maxdepth 1 -type d -name '20*' ! -name '*.part' | sort
}

# ─── backup ──────────────────────────────────────────────────────────────────

cmd_backup() {
  step "lancement d'une sauvegarde"
  if systemctl list-unit-files pg-backup.service >/dev/null 2>&1; then
    # Via systemd : la sortie part dans le journal, avec le même environnement
    # que les exécutions automatiques du timer.
    systemctl start pg-backup.service || {
      error "échec — journal :"
      journalctl -u pg-backup -n 20 --no-pager >&2
      exit 1
    }
    journalctl -u pg-backup -n 20 --no-pager
  else
    warn "unité pg-backup.service absente, appel direct du script"
    sudo -u postgres /usr/local/bin/pg-backup.sh
  fi
}

# ─── list ────────────────────────────────────────────────────────────────────

cmd_list() {
  local latest_target="" n=0
  [[ -L "${DEST}/latest" ]] && latest_target="$(readlink -f "${DEST}/latest")"

  printf '%-18s  %-10s  %-8s  %s\n' "INSTANTANÉ" "ÂGE" "TAILLE" "BASES"
  printf '%-18s  %-10s  %-8s  %s\n' "------------------" "----------" "--------" "-----"

  while IFS= read -r d; do
    [[ -n $d ]] || continue
    local name age size dbs mark
    name="$(basename "$d")"
    age="$(( ( $(date +%s) - $(stat -c %Y "$d") ) / 86400 ))j"
    size="$(du -sh --apparent-size "$d" 2>/dev/null | cut -f1)"
    if [[ -f "$d/MANIFEST" ]]; then
      dbs="$(awk -F': *' '/^bases/ {print $2}' "$d/MANIFEST")"
    else
      dbs="$(find "$d" -name '*.dump' -printf '%f ' | sed 's/\.dump//g')"
    fi
    mark=""
    [[ $d == "$latest_target" ]] && mark=" ← latest"
    printf '%-18s  %-10s  %-8s  %s%s\n' "$name" "$age" "$size" "$dbs" "$mark"
    n=$((n+1))
  done < <(snapshots)

  [[ $n -eq 0 ]] && warn "aucune sauvegarde dans ${DEST}"
  echo
  log "${n} sauvegarde(s), $(du -sh --apparent-size "$DEST" 2>/dev/null | cut -f1) — $(df -Pm "$DEST" | awk 'NR==2 {print $4}') Mo libres"
}

# ─── show ────────────────────────────────────────────────────────────────────

cmd_show() {
  local snap; snap="$(resolve "${1:-latest}")"
  step "$(basename "$snap")"
  [[ -f "$snap/MANIFEST" ]] && cat "$snap/MANIFEST" || warn "pas de MANIFEST"
  echo
  ls -lh "$snap"
}

# ─── restore ─────────────────────────────────────────────────────────────────

confirm() {
  [[ $ASSUME_YES -eq 1 ]] && return 0
  local answer
  read -r -p "$1 [tapez le nom de la base pour confirmer] : " answer
  [[ $answer == "$2" ]] || die "annulé"
}

cmd_restore() {
  local db="${1:-}" snap owner dump pre
  [[ -n $db ]] || die "usage : pgbk restore <base> [instantané]"
  snap="$(resolve "${2:-latest}")"
  dump="${snap}/${db}.dump"

  [[ -f $dump ]] || die "${db}.dump absent de $(basename "$snap") — voir 'pgbk show'"

  step "restauration de « ${db} » depuis $(basename "$snap")"
  [[ -f "$snap/MANIFEST" ]] && awk '{print "         " $0}' "$snap/MANIFEST"

  # Le propriétaire doit être capturé AVANT le dropdb : il disparaît avec la
  # base. À défaut, on retombe sur la convention « un rôle par base ».
  owner="$($PSQL -tAc "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='${db}'" || true)"
  [[ -n $owner ]] || owner="$db"
  log "propriétaire cible : ${owner}"

  if [[ $($PSQL -tAc "SELECT 1 FROM pg_roles WHERE rolname='${owner}'") != 1 ]]; then
    die "le rôle ${owner} n'existe pas — le recréer avant (voir globals.sql)"
  fi

  confirm "ÉCRASE la base ${db}" "$db"

  # Filet : un dump de l'état courant avant de le détruire. C'est la seule
  # protection contre « je me suis trompé d'instantané ».
  if [[ $($PSQL -tAc "SELECT 1 FROM pg_database WHERE datname='${db}'") == 1 ]]; then
    pre="${DEST}/pre-restore-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$pre"; chown postgres:postgres "$pre"; chmod 700 "$pre"
    step "filet de sécurité : dump de l'état courant"
    sudo -u postgres pg_dump -Fc --no-owner --no-acl "$db" > "${pre}/${db}.dump"
    log "  ${pre}/${db}.dump"

    step "fermeture des connexions à ${db}"
    $PSQL -tAc "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity \
                WHERE datname='${db}' AND pid <> pg_backend_pid()" \
      | xargs -I{} echo "         {} session(s) fermée(s)"

    sudo -u postgres dropdb "$db"
  else
    warn "la base ${db} n'existe pas — création"
  fi

  step "création de la base"
  sudo -u postgres createdb "$db" -O "$owner" -T template0 \
       --encoding UTF8 --lc-collate C --lc-ctype C

  step "chargement du dump"
  sudo -u postgres pg_restore -d "$db" --no-owner --role="$owner" "$dump"

  # Les ACL au niveau base ne sont ni dans le dump ni dans globals.sql : sans
  # cette étape, PUBLIC retrouve le droit CONNECT et l'isolation entre
  # locataires disparaît silencieusement.
  step "réapplication des ACL"
  $PSQL -v ON_ERROR_STOP=1 -q <<SQL
REVOKE CONNECT ON DATABASE "${db}" FROM PUBLIC;
GRANT  CONNECT ON DATABASE "${db}" TO "${owner}";
SQL
  $PSQL -d "$db" -v ON_ERROR_STOP=1 -q <<SQL
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER  SCHEMA public OWNER TO "${owner}";
GRANT  ALL ON SCHEMA public TO "${owner}";
SQL

  cmd_verify "$db"
  step "restauration terminée"
  [[ -n ${pre:-} ]] && log "état précédent conservé dans ${pre}"
}

# ─── verify ──────────────────────────────────────────────────────────────────

cmd_verify() {
  local db="${1:-}" acl tables bad
  [[ -n $db ]] || die "usage : pgbk verify <base>"

  step "contrôle de « ${db} »"

  acl="$($PSQL -tAc "SELECT coalesce(array_to_string(datacl, ' '), '') FROM pg_database WHERE datname='${db}'")"
  if [[ $acl == *"=Tc/"* || $acl == *"=c/"* ]]; then
    warn "  ACL : PUBLIC a le droit CONNECT — isolation absente"
  elif [[ -z $acl ]]; then
    warn "  ACL : privilèges par défaut, PUBLIC peut se connecter"
    warn "        REVOKE CONNECT ON DATABASE \"${db}\" FROM PUBLIC;"
  else
    log "  ACL : ${acl}"
  fi

  tables="$($PSQL -d "$db" -tAc "SELECT count(*) FROM pg_tables WHERE schemaname='public'")"
  log "  tables (schéma public) : ${tables}"

  bad="$($PSQL -d "$db" -tAc "SELECT count(*) FROM pg_tables \
        WHERE schemaname='public' AND tableowner <> '${db}'")"
  [[ $bad -gt 0 ]] \
    && warn "  ${bad} table(s) n'appartiennent pas à ${db} — pg_restore sans --role ?" \
    || log "  propriétaire des tables : OK"
}

# ─── Entrée ──────────────────────────────────────────────────────────────────

[[ $# -eq 0 ]] && usage 1

CMD="$1"; shift
[[ ${1:-} == --yes || ${2:-} == --yes ]] && ASSUME_YES=1
set -- "${@/--yes/}"

case "$CMD" in
  backup)  cmd_backup ;;
  list|ls) cmd_list ;;
  show)    cmd_show "${1:-latest}" ;;
  restore) cmd_restore "${@}" ;;
  verify)  cmd_verify "${1:-}" ;;
  -h|--help|help) usage ;;
  *)       die "commande inconnue : ${CMD} (voir pgbk --help)" ;;
esac
