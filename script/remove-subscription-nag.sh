#!/bin/bash
#
# Supprime la popup "No valid subscription" de l'interface web Proxmox VE / PBS.
#
# La condition de Proxmox.Utils.checked_command dans proxmoxlib.js est rendue
# toujours fausse : la popup n'est jamais affichee et orig_cmd() est appele
# directement (comportement identique a la branche "else" d'origine).
#
# La ligne a patcher est reperee par POSITION (la condition sur status juste
# avant le libelle "No valid subscription") et non par un motif textuel fige :
# Proxmox reformate ce bloc d'une version a l'autre (PVE 8 le coupe en deux
# lignes, PVE 9 met la condition sur sa propre ligne).
#
# Usage :
#   ./remove-subscription-nag.sh                 applique le patch
#   ./remove-subscription-nag.sh --status        etat du fichier
#   ./remove-subscription-nag.sh --restore       restaure le fichier d'origine
#   ./remove-subscription-nag.sh --install-hook  rejoue le patch apres chaque apt
#   ./remove-subscription-nag.sh --remove-hook   retire le hook APT
#
# A lancer en root sur l'hote PVE ou PBS.

set -euo pipefail

# Surchargeable pour les tests hors hote Proxmox.
JS=${PROXMOXLIB_JS:-/usr/share/javascript/proxmox-widget-toolkit/proxmoxlib.js}
ORIG="$JS.orig"
LEGACY_BAK="$JS.bak"
MARKER="nag-removed"
NAG_LABEL="No valid subscription"
HOOK=/etc/apt/apt.conf.d/99-remove-subscription-nag
INSTALL_PATH=/usr/local/sbin/remove-subscription-nag.sh
SERVICES=(pveproxy.service proxmox-backup-proxy.service)

log()  { printf '%s\n' "$*"; }
warn() { printf 'ATTENTION : %s\n' "$*" >&2; }
err()  { printf 'ERREUR : %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

require_root() {
    [[ $EUID -eq 0 ]] || die "Ce script doit etre lance en root."
}

require_file() {
    [[ -f $JS ]] || die "$JS introuvable : proxmox-widget-toolkit installe ?"
}

restart_web_services() {
    local svc restarted=0
    for svc in "${SERVICES[@]}"; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            systemctl restart "$svc"
            log "  $svc redemarre."
            restarted=1
        fi
    done
    if [[ $restarted -eq 0 ]]; then
        log "  Aucun service web actif, rien a redemarrer."
    else
        log "  Forcer le rechargement du navigateur (Ctrl+F5) pour voir l'effet."
    fi
}

# Numero de la ligne portant la condition sur le statut de souscription : la
# derniere occurrence de status.toLowerCase() dans les 25 lignes precedant le
# libelle de la popup (ou sur la meme ligne si le fichier est minifie).
locate_condition_line() {
    local nag start line
    nag=$(grep -n -- "$NAG_LABEL" "$JS" | head -1 | cut -d: -f1)
    [[ -n ${nag:-} ]] || return 1
    start=$(( nag > 25 ? nag - 25 : 1 ))
    line=$(awk -v s="$start" -v e="$nag" \
        'NR>=s && NR<=e && /status\.toLowerCase\(\)/ { last=NR } END { if (last) print last }' \
        "$JS")
    [[ -n $line ]] || return 1
    printf '%s\n' "$line"
}

# original       : condition intacte              (!== 'active')
# legacy-patched : patch de l'ancien one-liner    (=== 'active')
# patched        : patch de ce script             (=== 'nag-removed')
classify_line() {
    local content="$1"
    case "$content" in
        *"'$MARKER'"*)    printf 'patched\n' ;;
        *"!== 'active'"*) printf 'original\n' ;;
        *"=== 'active'"*) printf 'legacy-patched\n' ;;
        *)                printf 'unknown\n' ;;
    esac
}

backup_file() {
    local src="$1" dest
    [[ -e $src ]] || return 0
    dest="$src.$(date +%Y%m%d-%H%M%S).bak"
    cp -p "$src" "$dest"
    log "  snapshot : $dest"
}

# Conserve une copie pristine, une seule fois. Si l'ancien script a laisse un
# .bak et que celui-ci contient bien la condition d'origine, il fait l'affaire.
ensure_original_backup() {
    local state="$1"
    [[ -f $ORIG ]] && return 0

    if [[ $state == original ]]; then
        cp -p "$JS" "$ORIG"
        log "  original sauvegarde : $ORIG"
        return 0
    fi

    if [[ -f $LEGACY_BAK ]] && grep -qF -- "!== 'active'" "$LEGACY_BAK"; then
        cp -p "$LEGACY_BAK" "$ORIG"
        log "  original recupere depuis $LEGACY_BAK : $ORIG"
        return 0
    fi

    warn "aucune copie pristine disponible ($ORIG absent, $LEGACY_BAK inutilisable)."
    warn "--restore ne pourra pas fonctionner ; en cas de besoin :"
    warn "  apt install --reinstall proxmox-widget-toolkit"
}

do_status() {
    require_file
    local line content state
    if ! line=$(locate_condition_line); then
        log "Etat   : condition introuvable (libelle \"$NAG_LABEL\" absent ?)"
        return 0
    fi
    content=$(sed -n "${line}p" "$JS")
    state=$(classify_line "$content")
    log "Fichier : $JS"
    log "Ligne   : $line"
    log "Contenu : $(printf '%s' "$content" | sed 's/^[[:space:]]*//')"
    case "$state" in
        patched)        log "Etat    : patche par ce script (popup desactivee)" ;;
        legacy-patched) log "Etat    : patche par l'ancien one-liner (popup desactivee," \
                            "mais reapparaitrait avec une souscription valide)" ;;
        original)       log "Etat    : non patche (popup active)" ;;
        unknown)        log "Etat    : inconnu, inspection manuelle necessaire" ;;
    esac
    log "Backup  : $([[ -f $ORIG ]] && echo "$ORIG" || echo 'aucun')"
    log "Hook APT: $([[ -f $HOOK ]] && echo "$HOOK" || echo 'non installe')"
}

do_patch() {
    require_root
    require_file

    local line content state
    line=$(locate_condition_line) || die \
        "Impossible de localiser la condition de souscription dans $JS. Inspecter :
  grep -n -B6 -A3 \"$NAG_LABEL\" $JS"
    content=$(sed -n "${line}p" "$JS")
    state=$(classify_line "$content")

    case "$state" in
        patched)
            log "Deja patche (ligne $line), rien a faire."
            exit 0
            ;;
        unknown)
            err "La ligne $line ne correspond a aucune forme connue :"
            err "  $content"
            err "Le code amont a probablement change ; inspecter :"
            err "  grep -n -B6 -A3 \"$NAG_LABEL\" $JS"
            exit 2
            ;;
    esac

    log "Condition trouvee ligne $line ($state) :"
    log "  $(printf '%s' "$content" | sed 's/^[[:space:]]*//')"

    ensure_original_backup "$state"
    backup_file "$JS"
    local snapshot
    snapshot=$(ls -1t "$JS".*.bak 2>/dev/null | head -1)

    # Substitution limitee a la ligne reperee : aucun risque de toucher une
    # autre comparaison de statut ailleurs dans le fichier.
    if [[ $state == original ]]; then
        sed -i "${line}s/!== 'active'/=== '$MARKER'/" "$JS"
    else
        sed -i "${line}s/=== 'active'/=== '$MARKER'/" "$JS"
        log "  (ancien patch converti : la popup ne reviendra pas si une"
        log "   souscription valide est activee un jour)"
    fi

    if ! sed -n "${line}p" "$JS" | grep -qF -- "$MARKER"; then
        err "Le patch n'a pas pris, restauration du snapshot."
        [[ -n ${snapshot:-} ]] && cp -p "$snapshot" "$JS"
        exit 1
    fi
    log "Patch applique."
    log "  $(sed -n "${line}p" "$JS" | sed 's/^[[:space:]]*//')"

    restart_web_services
}

do_restore() {
    require_root
    [[ -f $ORIG ]] || die "Aucune sauvegarde $ORIG. Restauration possible via :
  apt install --reinstall proxmox-widget-toolkit"
    cp -p "$ORIG" "$JS"
    log "Fichier d'origine restaure."
    restart_web_services
}

do_install_hook() {
    require_root
    local src
    src=$(readlink -f "${BASH_SOURCE[0]}")
    if [[ $src != "$INSTALL_PATH" ]]; then
        install -m 0755 "$src" "$INSTALL_PATH"
        log "Script copie dans $INSTALL_PATH"
    fi
    # "|| true" indispensable : un Post-Invoke en echec fait echouer apt.
    cat > "$HOOK" <<EOF
// Rejoue le patch anti-popup apres chaque operation dpkg, car une mise a jour
// de proxmox-widget-toolkit remplace proxmoxlib.js par la version d'origine.
DPkg::Post-Invoke { "test -x $INSTALL_PATH && $INSTALL_PATH >/dev/null 2>&1 || true"; };
EOF
    log "Hook APT installe : $HOOK"
}

do_remove_hook() {
    require_root
    rm -f "$HOOK"
    log "Hook APT supprime ($HOOK)."
    log "Le script reste dans $INSTALL_PATH (a supprimer manuellement si besoin)."
}

case "${1:-}" in
    ""|--patch)     do_patch ;;
    --status)       do_status ;;
    --restore)      do_restore ;;
    --install-hook) do_install_hook ;;
    --remove-hook)  do_remove_hook ;;
    -h|--help)
        awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' \
            "${BASH_SOURCE[0]}"
        ;;
    *)
        die "Option inconnue : $1 (voir --help)"
        ;;
esac
