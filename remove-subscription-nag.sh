#!/bin/bash
#
# Supprime la popup "No valid subscription" de l'interface web Proxmox VE / PBS.
#
# Le patch modifie la condition de Proxmox.Utils.checked_command dans
# proxmoxlib.js pour qu'elle soit toujours fausse : la popup n'est jamais
# affichee et orig_cmd() est appele directement (comportement identique a la
# branche "else" d'origine).
#
# Usage :
#   ./remove-subscription-nag.sh                 applique le patch
#   ./remove-subscription-nag.sh --restore       restaure le fichier d'origine
#   ./remove-subscription-nag.sh --install-hook  rejoue le patch apres chaque apt
#   ./remove-subscription-nag.sh --remove-hook   retire le hook APT
#
# A lancer en root sur l'hote PVE ou PBS.

set -euo pipefail

JS=/usr/share/javascript/proxmox-widget-toolkit/proxmoxlib.js
ORIG="$JS.orig"
PATTERN=".data.status.toLowerCase() !== 'active'"
MARKER="nag-removed"
HOOK=/etc/apt/apt.conf.d/99-remove-subscription-nag
INSTALL_PATH=/usr/local/sbin/remove-subscription-nag.sh
SERVICES=(pveproxy.service proxmox-backup-proxy.service)

log() { printf '%s\n' "$*"; }
err() { printf '%s\n' "$*" >&2; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        err "Ce script doit etre lance en root."
        exit 1
    fi
}

restart_web_services() {
    local svc restarted=0
    for svc in "${SERVICES[@]}"; do
        if systemctl is-active --quiet "$svc"; then
            systemctl restart "$svc"
            log "  $svc redemarre."
            restarted=1
        fi
    done
    if [[ $restarted -eq 0 ]]; then
        log "  Aucun service web actif, rien a redemarrer."
    else
        log "  Pensez a forcer le rechargement du navigateur (Ctrl+F5)."
    fi
}

do_patch() {
    require_root

    if [[ ! -f $JS ]]; then
        err "$JS introuvable : proxmox-widget-toolkit n'est pas installe ?"
        exit 1
    fi

    if ! grep -qF -- "$PATTERN" "$JS"; then
        if grep -qF -- "$MARKER" "$JS"; then
            log "Deja patche, rien a faire."
            exit 0
        fi
        err "Motif introuvable et marqueur absent : le format de proxmoxlib.js"
        err "a probablement change. Verifier avec :"
        err "  grep -n -B3 -A3 \"No valid subscription\" $JS"
        exit 2
    fi

    # L'original n'est sauvegarde qu'une seule fois : -n empeche d'ecraser une
    # sauvegarde saine par une copie deja patchee lors d'un second passage.
    if [[ ! -f $ORIG ]]; then
        cp -p "$JS" "$ORIG"
        log "Original sauvegarde dans $ORIG"
    fi
    local snapshot="$JS.$(date +%Y%m%d-%H%M%S).bak"
    cp -p "$JS" "$snapshot"
    log "Snapshot : $snapshot"

    sed -i \
        "s/\.data\.status\.toLowerCase() !== 'active'/.data.status.toLowerCase() === '$MARKER'/" \
        "$JS"

    if ! grep -qF -- "$MARKER" "$JS"; then
        err "Le patch n'a pas ete applique, restauration."
        cp -p "$snapshot" "$JS"
        exit 1
    fi
    log "Patch applique."

    restart_web_services
}

do_restore() {
    require_root

    if [[ ! -f $ORIG ]]; then
        err "Aucune sauvegarde $ORIG. Restauration possible via :"
        err "  apt install --reinstall proxmox-widget-toolkit"
        exit 1
    fi

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
    log "Le script reste en place dans $INSTALL_PATH (a supprimer manuellement)."
}

case "${1:-}" in
    ""|--patch)   do_patch ;;
    --restore)    do_restore ;;
    --install-hook) do_install_hook ;;
    --remove-hook)  do_remove_hook ;;
    -h|--help)
        # bloc de commentaires en tete, hors shebang, sans plage codee en dur
        awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' \
            "${BASH_SOURCE[0]}"
        ;;
    *)
        err "Option inconnue : $1 (voir --help)"
        exit 1
        ;;
esac
