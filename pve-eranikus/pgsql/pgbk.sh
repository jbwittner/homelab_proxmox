#!/usr/bin/env bash
#
# pgbk — gestion des sauvegardes PostgreSQL du cluster mutualisé.
#
#   pgbk backup                       lance une sauvegarde
#   pgbk list                         liste les sauvegardes disponibles
#   pgbk show [instantané]            détail d'une sauvegarde
#   pgbk restore <base> [instantané]  restaure une base
#   pgbk verify <base>                contrôle l'état d'une base restaurée
#   pgbk delete <instantané>          supprime une sauvegarde
#
# L'instantané se désigne par :
#   latest            la plus récente (défaut)
#   20260820-093240   horodatage exact
#   20260820          la plus récente de ce jour
#
# Options :
#   --ctid ID   conteneur cible, prioritaire sur $PG_CTID et sur /etc/default/pgbk
#   --yes       pas de demande de confirmation sur un restore ou un delete
#   --plan      delete : dit ce qui serait supprimé, n'efface rien
#   --local     force le mode moteur (n'essaie pas de déléguer)
#
# Le dernier instantané — celui vers lequel « latest » pointe — ne peut pas
# être supprimé : ce serait laisser le cluster sans filet. La rétention de
# pg-backup.sh est là pour faire le ménage, delete est pour les cas ponctuels.
#
# UN SEUL FICHIER, DEUX RÔLES. Le même script est posé sur le nœud Proxmox et
# dans le conteneur, et se comporte selon l'endroit où il tourne :
#
#   sur le nœud (pct présent)  il confirme, puis délègue au CT et s'efface
#   dans le CT (pas de pct)    il fait le travail
#
# Il n'y a donc pas de « version hôte » et de « version CT » à ne pas
# confondre : c'est le même contenu aux deux endroits, et pg-deploy.sh l'y pose.
#
# pg-backup.sh reste le moteur des sauvegardes, appelé par le timer. pgbk est
# l'interface humaine : il n'écrit aucune sauvegarde lui-même, il orchestre.
#
set -Eeuo pipefail

CONF=/etc/default/pgbk          # PG_CTID, écrit par pg-deploy.sh
CT_PGBK=/usr/local/bin/pgbk     # chemin du script DANS le conteneur
DEST="${PG_BACKUP_DEST:-/var/backups/postgresql}"
PSQL="sudo -u postgres psql"
ASSUME_YES=0
LOCAL=0
PLAN=0

log()   { printf '%s [INFO ] %s\n'  "$(date '+%H:%M:%S')" "$*"; }
warn()  { printf '%s [WARN ] %s\n'  "$(date '+%H:%M:%S')" "$*" >&2; }
error() { printf '%s [ERROR] %s\n'  "$(date '+%H:%M:%S')" "$*" >&2; }
step()  { printf '%s [STEP ] %s\n'  "$(date '+%H:%M:%S')" "$*"; }
die()   { error "$*"; exit 1; }

usage() {
  awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
  exit "${1:-0}"
}

# ─── Arguments ───────────────────────────────────────────────────────────────
# Les drapeaux sont retirés de $@ quelle que soit leur position : un filtrage
# par substitution laisserait un argument vide, et « restore --yes base »
# arriverait dans cmd_restore avec une base vide.

CTID_ENV=${PG_CTID:-}
CTID_FLAG=
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes)   ASSUME_YES=1; shift ;;
    --local) LOCAL=1; shift ;;
    --plan)  PLAN=1; shift ;;
    --ctid)  [[ $# -ge 2 ]] || die "--ctid attend une valeur"
             CTID_FLAG=$2; shift 2 ;;
    *)       ARGS+=("$1"); shift ;;
  esac
done

# L'aide sort avant tout contrôle : lisible sans être root, et depuis
# n'importe quelle machine.
[[ ${#ARGS[@]} -eq 0 ]] && usage 1
case "${ARGS[0]}" in -h|--help|help) usage ;; esac

set -- "${ARGS[@]}"
CMD="$1"; shift

# ─── Mode hôte : confirmer, déléguer, s'effacer ──────────────────────────────
# Détection par la présence de pct : un nœud Proxmox l'a, le conteneur Debian
# non. --local (ou PGBK_LOCAL=1) force le mode moteur.

if [[ $LOCAL -eq 0 && ${PGBK_LOCAL:-0} -eq 0 ]] && command -v pct >/dev/null 2>&1; then

  [[ $EUID -eq 0 ]] || die "à lancer en root sur le nœud (pct l'exige)"

  # Priorité : --ctid, puis l'environnement, puis le CTID consigné par pg-deploy.
  # shellcheck source=/dev/null
  [[ -r $CONF ]] && . "$CONF"
  CTID=${CTID_FLAG:-${CTID_ENV:-${PG_CTID:-}}}

  [[ -n $CTID ]] || die "aucun conteneur cible : ${CONF} absent ou sans PG_CTID
         le consigner  : pg-deploy.sh --ctid <ID>
         ou ponctuel   : pgbk --ctid <ID> ${CMD} $*"
  [[ $CTID =~ ^[0-9]+$ ]] || die "CTID invalide : ${CTID}"

  pct config "$CTID" >/dev/null 2>&1 || die "CT ${CTID} inexistant"
  [[ $(pct status "$CTID" 2>/dev/null | awk '{print $2}') == running ]] \
    || die "CT ${CTID} à l'arrêt — le démarrer : pct start ${CTID}"
  pct exec "$CTID" -- test -x "$CT_PGBK" 2>/dev/null \
    || die "${CT_PGBK} absent du CT ${CTID} — le poser : pg-deploy.sh"

  # pct exec n'alloue pas de TTY : le read du script côté CT ne verrait jamais
  # la saisie, et la question de sécurité de restore serait muette. Le
  # garde-fou est donc posé ici, où le terminal existe, puis --yes est passé.
  if [[ $ASSUME_YES -eq 0 && ( $CMD == restore || $CMD == delete || $CMD == rm ) ]]; then
    case "$CMD" in
      restore)
        db=""
        for a in "$@"; do [[ $a == --* ]] && continue; db=$a; break; done
        [[ -n $db ]] || die "usage : pgbk restore <base> [instantané]"
        read -r -p "ÉCRASE la base ${db} du CT ${CTID} [tapez le nom de la base pour confirmer] : " answer
        [[ $answer == "$db" ]] || die "annulé"
        ;;
      delete|rm)
        # Le CT seul sait à quoi une référence correspond : « 20260819 » désigne
        # la plus récente de ce jour, qui peut être le dernier instantané.
        # --plan applique toutes les gardes et n'efface rien ; la question porte
        # donc sur ce qui sera réellement supprimé, pas sur ce qui a été tapé.
        target="$(pct exec "$CTID" -- "$CT_PGBK" "$CMD" "$@" --plan)" || exit $?
        [[ -n $target ]] || die "rien à supprimer"
        read -r -p "SUPPRIME l'instantané ${target} du CT ${CTID} [tapez son nom pour confirmer] : " answer
        [[ $answer == "$target" ]] || die "annulé"
        ;;
    esac
    ASSUME_YES=1
  fi

  # exec : le code de retour du CT devient celui de cette commande.
  if [[ $ASSUME_YES -eq 1 ]]; then
    exec pct exec "$CTID" -- "$CT_PGBK" "$CMD" "$@" --yes
  fi
  exec pct exec "$CTID" -- "$CT_PGBK" "$CMD" "$@"
fi

# ─── Mode moteur : on est dans le conteneur ──────────────────────────────────

[[ $EUID -eq 0 ]] || die "à lancer en root : « pgbk » depuis le nœud, ou dans le CT après « pct enter »"

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
  local answer what="${3:-le nom de la base}"
  read -r -p "$1 [tapez ${what} pour confirmer] : " answer
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

# ─── delete ──────────────────────────────────────────────────────────────────

# Détails de la cible. Sur stderr en mode --plan, pour que stdout ne porte que
# le nom résolu — seule chose que l'appelant capture.
delete_details() {
  local snap="$1" name="$2"
  step "instantané visé : ${name}"
  [[ -f "$snap/MANIFEST" ]] && awk '{print "         " $0}' "$snap/MANIFEST"
  log "  taille : $(du -sh --apparent-size "$snap" 2>/dev/null | cut -f1)"
}

cmd_delete() {
  local ref="${1:-}" snap name latest_target dest_real
  [[ -n $ref ]] || die "usage : pgbk delete <instantané>"

  # « latest » est un alias, pas une cible. Le refuser par son nom donne un
  # message clair ; la garde qui compte est la comparaison de chemins plus bas,
  # car « pgbk delete 20260819 » peut désigner le dernier instantané sans
  # jamais prononcer le mot.
  [[ $ref == latest ]] \
    && die "le dernier instantané est protégé — il n'y a rien à supprimer sous ce nom"

  # Résolution en DEUX TEMPS, délibérément.
  #
  # « resolve » meurt sur une référence inconnue, et ce die doit tuer le script.
  # L'imbriquer dans une autre substitution — readlink -f "$(resolve …)" —
  # l'avalerait : le sous-shell meurt, la substitution rend une chaîne vide, et
  # « readlink -f "" » renvoie LE RÉPERTOIRE COURANT avec un code 0. Toutes les
  # gardes ci-dessous seraient alors satisfaites et le rm -rf final emporterait
  # le répertoire de travail. Ne jamais fusionner ces deux étapes.
  if [[ -d "${DEST}/${ref}" ]]; then
    snap="${DEST}/${ref}"
  else
    snap="$(resolve "$ref")"
  fi
  [[ -n $snap ]] || die "référence non résolue : ${ref}"
  snap="$(readlink -f -- "$snap")"
  [[ -n $snap && -d $snap ]] || die "instantané introuvable : ${ref}"

  # Ceinture et bretelles : quoi qu'ait donné la résolution, rien en dehors de
  # DEST n'est supprimable. Couvre aussi « delete ../../quelque-chose ».
  dest_real="$(readlink -f -- "$DEST")"
  [[ -n $dest_real && $snap == "$dest_real"/* ]] \
    && [[ $snap != "$dest_real" ]] \
    || die "hors de ${DEST} : ${snap} — refus"

  name="$(basename "$snap")"

  [[ $name == *.part ]] \
    && die "${name} est une exécution en cours ou interrompue — pg-backup.sh nettoie ces débris lui-même"

  latest_target=""
  [[ -L "${DEST}/latest" ]] && latest_target="$(readlink -f -- "${DEST}/latest")"
  if [[ -z $latest_target ]]; then
    # Sans le lien, la plus récente en tient lieu : l'intention est la même, et
    # un lien cassé ne doit pas ouvrir la porte à la suppression du dernier.
    latest_target="$(snapshots | tail -1)"
    [[ -n $latest_target ]] \
      && warn "${DEST}/latest absent — protection reportée sur $(basename "$latest_target")"
  fi

  [[ -n $latest_target && $snap == "$latest_target" ]] \
    && die "${name} est le dernier instantané — protégé.
         Supprimer la dernière sauvegarde laisserait le cluster sans filet.
         Lancer « pgbk backup » d'abord si le but est de la remplacer."

  if [[ $PLAN -eq 1 ]]; then
    delete_details "$snap" "$name" >&2
    echo "$name"
    return 0
  fi

  delete_details "$snap" "$name"
  confirm "SUPPRIME l'instantané ${name}" "$name" "son nom"

  rm -rf -- "$snap"
  step "supprimé : ${name}"
  log "  $(snapshots | wc -l) sauvegarde(s) restante(s), $(df -Pm "$DEST" | awk 'NR==2 {print $4}') Mo libres"
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

case "$CMD" in
  backup)  cmd_backup ;;
  list|ls) cmd_list ;;
  show)    cmd_show "${1:-latest}" ;;
  restore) cmd_restore "$@" ;;
  verify)  cmd_verify "${1:-}" ;;
  delete|rm) cmd_delete "${1:-}" ;;
  *)       die "commande inconnue : ${CMD} (voir pgbk --help)" ;;
esac
