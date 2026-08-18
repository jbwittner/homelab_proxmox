#!/bin/bash
#
# Durcissement SSH d'un hote Proxmox VE / PBS : authentification par cle
# uniquement, mot de passe desactive, avec ajout de cles publiques.
#
# Le durcissement est depose dans un drop-in /etc/ssh/sshd_config.d/ et non
# dans sshd_config, pour survivre aux mises a jour du paquet openssh-server.
#
# Usage :
#   ./proxmox-ssh-setup.sh --add-key "ssh-ed25519 AAAA... moi@portable"
#   ./proxmox-ssh-setup.sh --key-file ~/id_ed25519.pub
#   cat cle.pub | ./proxmox-ssh-setup.sh --key-file -
#   ./proxmox-ssh-setup.sh --keys-only          (aucune cle a ajouter)
#   ./proxmox-ssh-setup.sh --status             etat effectif de sshd
#   ./proxmox-ssh-setup.sh --revert             reactive le mot de passe
#
# Options :
#   --add-key KEY     cle publique litterale (repetable)
#   --key-file FILE   fichier de cles publiques, "-" pour stdin (repetable)
#   --user USER       compte destinataire des cles (defaut : root)
#   --keys-only       durcit sshd sans ajouter de cle
#   --add-only        ajoute les cles sans toucher a la config sshd
#   --force           durcit meme sans aucune cle presente (DANGEREUX)
#   --no-reload       n'applique pas la config (verification seule)
#
# A lancer en root sur l'hote.

set -euo pipefail

DROPIN=/etc/ssh/sshd_config.d/01-proxmox-hardening.conf
SSHD_CONFIG=/etc/ssh/sshd_config
BACKUP_DIR=/root/ssh-setup-backups
TARGET_USER=root
MODE=full          # full | keys-only | add-only | status | revert
FORCE=0
RELOAD=1
KEYS=()

log()  { printf '%s\n' "$*"; }
warn() { printf 'ATTENTION : %s\n' "$*" >&2; }
err()  { printf 'ERREUR : %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

usage() {
    awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' \
        "${BASH_SOURCE[0]}"
}

# ---------------------------------------------------------------- arguments

while [[ $# -gt 0 ]]; do
    case "$1" in
        --add-key)
            [[ $# -ge 2 ]] || die "--add-key attend une valeur."
            KEYS+=("$2"); shift 2 ;;
        --key-file)
            [[ $# -ge 2 ]] || die "--key-file attend un chemin."
            if [[ $2 == "-" ]]; then
                mapfile -t -O "${#KEYS[@]}" KEYS < /dev/stdin
            else
                [[ -r $2 ]] || die "Fichier illisible : $2"
                mapfile -t -O "${#KEYS[@]}" KEYS < "$2"
            fi
            shift 2 ;;
        --user)
            [[ $# -ge 2 ]] || die "--user attend un nom de compte."
            TARGET_USER="$2"; shift 2 ;;
        --keys-only)  MODE=keys-only; shift ;;
        --add-only)   MODE=add-only;  shift ;;
        --status)     MODE=status;    shift ;;
        --revert)     MODE=revert;    shift ;;
        --force)      FORCE=1;        shift ;;
        --no-reload)  RELOAD=0;       shift ;;
        -h|--help)    usage; exit 0 ;;
        *)            die "Option inconnue : $1 (voir --help)" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "Ce script doit etre lance en root."

# ------------------------------------------------------------------ helpers

# Etat effectif de sshd, toutes inclusions et drop-ins resolus.
sshd_effective() {
    /usr/sbin/sshd -T 2>/dev/null | tr '[:upper:]' '[:lower:]'
}

show_status() {
    local eff
    eff=$(sshd_effective) || die "sshd -T a echoue : configuration invalide ?"
    log "Configuration effective de sshd :"
    local d
    for d in permitrootlogin pubkeyauthentication passwordauthentication \
             kbdinteractiveauthentication permitemptypasswords maxauthtries; do
        printf '  %-32s %s\n' "$d" "$(grep -m1 "^$d " <<<"$eff" | cut -d' ' -f2-)"
    done

    log ""
    log "Cles autorisees :"
    local u f n
    for u in root "$TARGET_USER"; do
        f=$(authorized_keys_path "$u" 2>/dev/null) || continue
        [[ -f $f ]] || { printf '  %-12s (aucun fichier)\n' "$u"; continue; }
        n=$(grep -cE '^[^#[:space:]]' "$f" || true)
        printf '  %-12s %s cle(s) dans %s\n' "$u" "$n" "$f"
    done | sort -u
}

# Resout le authorized_keys d'un compte. Sur Proxmox, celui de root est un
# lien symbolique vers /etc/pve/priv/authorized_keys (pmxcfs, systeme FUSE
# repliqué dans le cluster) : on ecrit a travers le lien, jamais en le
# remplacant, et sans chmod/chown qui echoueraient sur FUSE.
authorized_keys_path() {
    local user="$1" home
    home=$(getent passwd "$user" | cut -d: -f6) \
        || die "Compte inconnu : $user"
    [[ -n $home ]] || die "Pas de repertoire home pour $user."
    printf '%s/.ssh/authorized_keys\n' "$home"
}

is_on_pmxcfs() {
    local resolved
    resolved=$(readlink -f "$1" 2>/dev/null || printf '%s' "$1")
    [[ $resolved == /etc/pve/* ]]
}

# Empreinte de la cle si elle est valide, sinon echec.
key_fingerprint() {
    local key="$1" tmp fp
    tmp=$(mktemp)
    printf '%s\n' "$key" > "$tmp"
    if fp=$(ssh-keygen -l -f "$tmp" 2>/dev/null); then
        rm -f "$tmp"
        printf '%s\n' "$fp"
        return 0
    fi
    rm -f "$tmp"
    return 1
}

# Deux premiers champs = type + material : le commentaire ne compte pas dans
# la comparaison, sinon la meme cle re-ajoutee avec un autre commentaire
# creerait un doublon.
key_material() { awk '{ print $1, $2 }' <<<"$1"; }

count_valid_keys() {
    local file="$1" n=0 line
    [[ -f $file ]] || { printf '0\n'; return 0; }
    while IFS= read -r line; do
        if [[ -z ${line// /} || $line == \#* ]]; then
            continue
        fi
        if key_fingerprint "$line" >/dev/null; then
            n=$((n + 1))
        fi
    done < "$file"
    printf '%s\n' "$n"
}

backup_file() {
    local src="$1" dest
    [[ -e $src ]] || return 0
    mkdir -p "$BACKUP_DIR"
    dest="$BACKUP_DIR/$(basename "$src").$(date +%Y%m%d-%H%M%S)"
    cp -L "$src" "$dest"
    log "  sauvegarde : $dest"
}

# --------------------------------------------------------------- ajout cles

add_keys() {
    local file dir line fp mat added=0 skipped=0
    file=$(authorized_keys_path "$TARGET_USER")
    dir=$(dirname "$file")

    if [[ ! -d $dir ]]; then
        mkdir -p "$dir"
        chmod 700 "$dir"
        chown "$TARGET_USER": "$dir"
        log "Repertoire cree : $dir"
    fi

    if [[ -f $file ]]; then
        backup_file "$file"
    else
        # Creation a travers le lien symbolique s'il en existe un.
        : >> "$file"
    fi

    for line in "${KEYS[@]}"; do
        if [[ -z ${line// /} || $line == \#* ]]; then
            continue
        fi
        if ! fp=$(key_fingerprint "$line"); then
            warn "cle publique invalide, ignoree : ${line:0:40}..."
            continue
        fi
        mat=$(key_material "$line")
        if grep -qF -- "$mat" "$file" 2>/dev/null; then
            log "  deja presente : $fp"
            skipped=$((skipped + 1))
            continue
        fi
        # Append a travers le lien : ne jamais utiliser mv/sed -i ici, cela
        # remplacerait le symlink vers /etc/pve par un fichier local.
        printf '%s\n' "$line" >> "$file"
        log "  ajoutee : $fp"
        added=$((added + 1))
    done

    if is_on_pmxcfs "$file"; then
        log "  $file est sur pmxcfs (/etc/pve) : permissions gerees par Proxmox."
    else
        chmod 600 "$file"
        chown "$TARGET_USER": "$file"
    fi

    log "Cles : $added ajoutee(s), $skipped deja presente(s)."
}

# ------------------------------------------------------------- durcissement

check_include() {
    if grep -qE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' \
        "$SSHD_CONFIG"; then
        return 0
    fi
    warn "$SSHD_CONFIG ne contient pas la directive Include des drop-ins."
    log "  Ajout de l'Include en tete de fichier."
    backup_file "$SSHD_CONFIG"
    # En tete : dans sshd, la premiere valeur obtenue gagne, le drop-in doit
    # donc etre lu avant les directives du fichier principal.
    printf 'Include /etc/ssh/sshd_config.d/*.conf\n' > "$SSHD_CONFIG.new"
    cat "$SSHD_CONFIG" >> "$SSHD_CONFIG.new"
    mv "$SSHD_CONFIG.new" "$SSHD_CONFIG"
    chmod 644 "$SSHD_CONFIG"
}

# Sans cela, un sshd deja invalide pour une raison etrangere au script ferait
# echouer la verification post-ecriture et laisserait croire que le drop-in est
# en cause.
precheck_sshd() {
    local out
    if out=$(/usr/sbin/sshd -t 2>&1); then
        return 0
    fi
    err "La configuration sshd est DEJA invalide avant toute modification :"
    printf '%s\n' "$out" | sed 's/^/  /' >&2
    err "Corriger ce probleme avant de relancer le durcissement."
    exit 1
}

write_dropin() {
    mkdir -p "$(dirname "$DROPIN")"
    if [[ -f $DROPIN ]]; then
        backup_file "$DROPIN"
    fi
    cat > "$DROPIN" <<'EOF'
# Genere par proxmox-ssh-setup.sh : authentification par cle uniquement.
#
# Nomme 01-* pour etre lu avant les autres drop-ins (cloud-init depose un
# 50-cloud-init.conf avec PasswordAuthentication yes) : dans sshd, c'est la
# premiere valeur obtenue qui gagne.

# root reste autorise, mais par cle uniquement : Proxmox et les operations
# de cluster passent par des connexions SSH root.
PermitRootLogin prohibit-password

PubkeyAuthentication yes
PasswordAuthentication no

# Sans cela, PAM offre encore le mot de passe via keyboard-interactive et
# PasswordAuthentication no ne suffit pas a le bloquer.
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no

PermitEmptyPasswords no
MaxAuthTries 3
LoginGraceTime 30
EOF
    chmod 644 "$DROPIN"
    log "Drop-in ecrit : $DROPIN"
}

verify_and_reload() {
    if ! /usr/sbin/sshd -t; then
        err "Configuration sshd invalide, annulation."
        rm -f "$DROPIN"
        log "  $DROPIN supprime, la configuration precedente est intacte."
        exit 1
    fi
    log "Syntaxe sshd valide."

    local eff
    eff=$(sshd_effective)
    local pw kbd pk root
    pw=$(grep -m1 '^passwordauthentication ' <<<"$eff" | cut -d' ' -f2)
    kbd=$(grep -m1 '^kbdinteractiveauthentication ' <<<"$eff" | cut -d' ' -f2)
    pk=$(grep -m1 '^pubkeyauthentication ' <<<"$eff" | cut -d' ' -f2)
    root=$(grep -m1 '^permitrootlogin ' <<<"$eff" | cut -d' ' -f2)

    [[ $pk == yes ]]  || die "PubkeyAuthentication effectif = $pk (attendu yes)."
    [[ $pw == no ]]   || die "PasswordAuthentication effectif = $pw : une autre directive prend le dessus."
    [[ $kbd == no ]]  || die "KbdInteractiveAuthentication effectif = $kbd : le mot de passe reste joignable via PAM."
    # sshd -T renvoie l'alias historique without-password pour prohibit-password.
    case "$root" in
        prohibit-password|without-password) ;;
        *) die "PermitRootLogin effectif = $root (attendu prohibit-password)." ;;
    esac
    log "Etat effectif verifie : mot de passe desactive, cle publique active."

    if grep -qiE '^[[:space:]]*Match[[:space:]]' "$SSHD_CONFIG" \
        /etc/ssh/sshd_config.d/*.conf 2>/dev/null; then
        warn "des blocs Match existent : ils peuvent reactiver le mot de passe"
        warn "pour certains utilisateurs ou reseaux. Verifier avec :"
        warn "  sshd -T -C user=root,host=localhost,addr=127.0.0.1"
    fi

    if [[ $RELOAD -eq 0 ]]; then
        log "--no-reload : configuration ecrite mais non appliquee."
        return 0
    fi

    local unit
    for unit in ssh.service sshd.service; do
        if systemctl is-active --quiet "$unit" 2>/dev/null; then
            # reload plutot que restart : les sessions en cours survivent,
            # ce qui laisse un filet si la nouvelle config est mauvaise.
            if systemctl reload "$unit"; then
                log "$unit recharge."
            else
                warn "echec du rechargement de $unit, verifier : systemctl status $unit"
            fi
            break
        fi
    done
    # Debian 13 / PVE 9 : sshd est active par socket, chaque connexion relit
    # la config, mais on rafraichit l'unite socket si elle est en place.
    if systemctl is-active --quiet ssh.socket 2>/dev/null; then
        log "ssh.socket actif : la nouvelle config s'applique aux prochaines connexions."
    fi
}

do_revert() {
    [[ -f $DROPIN ]] || die "$DROPIN absent : rien a annuler."
    backup_file "$DROPIN"
    rm -f "$DROPIN"
    log "Drop-in supprime."
    if /usr/sbin/sshd -t; then
        if [[ $RELOAD -eq 1 ]] && systemctl is-active --quiet ssh.service; then
            systemctl reload ssh.service && log "ssh.service recharge."
        fi
        log "Le mot de passe redevient soumis a la configuration par defaut."
    else
        die "sshd -t echoue apres suppression : inspecter $SSHD_CONFIG."
    fi
}

# --------------------------------------------------------------------- main

case "$MODE" in
    status) show_status; exit 0 ;;
    revert) do_revert;   exit 0 ;;
esac

if [[ ${#KEYS[@]} -gt 0 ]]; then
    log "== Ajout des cles publiques pour $TARGET_USER"
    add_keys
    log ""
elif [[ $MODE == add-only ]]; then
    die "--add-only sans aucune cle fournie."
fi

if [[ $MODE == add-only ]]; then
    log "--add-only : sshd non modifie."
    exit 0
fi

log "== Verification des cles existantes"
AK=$(authorized_keys_path "$TARGET_USER")
NKEYS=$(count_valid_keys "$AK")
ROOT_AK=$(authorized_keys_path root)
NROOT=$(count_valid_keys "$ROOT_AK")
log "  $TARGET_USER : $NKEYS cle(s) valide(s) dans $AK"
if [[ $ROOT_AK != "$AK" ]]; then
    log "  root : $NROOT cle(s) valide(s) dans $ROOT_AK"
fi

# Verrou anti-lockout : desactiver le mot de passe sans aucune cle utilisable
# rend l'hote injoignable en SSH.
if [[ $NKEYS -eq 0 && $NROOT -eq 0 ]]; then
    if [[ $FORCE -eq 1 ]]; then
        warn "aucune cle autorisee, mais --force est actif : poursuite."
        warn "l'acces SSH sera impossible tant qu'aucune cle ne sera ajoutee."
    else
        err "Aucune cle publique valide n'est autorisee sur cet hote."
        err "Desactiver le mot de passe maintenant couperait tout acces SSH."
        err "Ajouter une cle d'abord :"
        err "  $0 --add-key \"ssh-ed25519 AAAA... moi@portable\""
        err "Passer outre en connaissance de cause : --force"
        exit 1
    fi
fi

log ""
log "== Durcissement de sshd"
precheck_sshd
check_include
write_dropin
verify_and_reload

log ""
log "Termine. Avant de fermer cette session, ouvrir une NOUVELLE connexion"
log "et verifier qu'elle aboutit :"
log "  ssh -o PreferredAuthentications=publickey $TARGET_USER@\$(hostname -f)"
log "En cas de probleme, la console noVNC de Proxmox reste accessible, et"
log "  $0 --revert   retablit l'authentification par mot de passe."
