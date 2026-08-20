#!/bin/bash
#
# Deploie le CT PostgreSQL mutualise depuis l'hote Proxmox : point de montage
# du depot, symlinks de configuration, unites systemd de sauvegarde, et pgbk
# sur l'hote et dans le conteneur.
#
# PREMIERE POSE ET MISES A JOUR, c'est le meme script. Les fichiers de
# configuration sont des symlinks vers le depot et suivent donc un git pull
# tout seuls, mais les scripts et les unites sont des COPIES : modifier
# pgbk.sh ou pg-backup.sh dans le depot ne change rien tant qu'on ne rejoue
# pas ce script. L'enchainer a chaque git pull est le geste normal :
#
#   cd /root/homelab_proxmox && git pull
#   pve-eranikus/pgsql/pg-deploy.sh
#
# Rejouable a l'identique : chaque etape est conditionnelle, rien n'est touche
# si l'etat est deja conforme.
#
# Le CTID retenu est consigne dans /etc/default/pgbk, d'ou pgbk le relit.
# Changer de conteneur ne demande donc que de rejouer ce script avec --ctid :
# il n'y a pas de second endroit a mettre a jour.
#
# pgbk.sh est pose a l'identique aux deux endroits — /usr/local/sbin/pgbk sur
# le noeud, /usr/local/bin/pgbk dans le CT. Un seul contenu, qui se comporte
# selon l'endroit ou il tourne.
#
# Usage :
#   ./pg-deploy.sh                deploiement complet (pose ou mise a jour)
#   ./pg-deploy.sh --status       etat de chaque element, ne change rien
#   ./pg-deploy.sh --dry-run      affiche ce qui serait fait
#   ./pg-deploy.sh --ctid 201     cible un autre conteneur, et le consigne
#   ./pg-deploy.sh --restart      force un restart de postgresql
#   ./pg-deploy.sh --no-container saute les prerequis conteneur (mp1, protection)
#
# A lancer en root sur le noeud Proxmox, pas dans le CT.

set -euo pipefail

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)   # source reelle du mp1
MP=/etc/pgsql-git                                    # cible du montage dans le CT
HOST_PGBK=/usr/local/sbin/pgbk
CONF=/etc/default/pgbk                               # CTID consigne, relu par pgbk
WAIT_TIMEOUT=120

# Priorite : --ctid, puis l'environnement, puis le CTID deja consigne, puis 200.
# Ce script doit pouvoir amorcer une installation vierge, il garde donc un
# defaut — contrairement a pgbk, qui refuse de deviner.
CTID_ENV=${PG_CTID:-}
CTID_FLAG=
# shellcheck source=/dev/null
[[ -r $CONF ]] && . "$CONF"
CTID=${CTID_ENV:-${PG_CTID:-200}}

MODE=apply         # apply | status
DRY=0
FORCE_RESTART=0
DO_CONTAINER=1

log()  { printf '%s\n' "$*"; }
warn() { printf 'ATTENTION : %s\n' "$*" >&2; }
err()  { printf 'ERREUR : %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

usage() {
    awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' \
        "${BASH_SOURCE[0]}"
}

# Une ligne par element, lue d'un coup d'oeil en fin de course.
SUMMARY=()
note() { SUMMARY+=("$(printf '  %-8s %s' "$1" "$2")"); }

# ---------------------------------------------------------------- arguments

while [[ $# -gt 0 ]]; do
    case "$1" in
        --status)       MODE=status; shift ;;
        --dry-run)      DRY=1; shift ;;
        --restart)      FORCE_RESTART=1; shift ;;
        --no-container) DO_CONTAINER=0; shift ;;
        --ctid)
            [[ $# -ge 2 ]] || die "--ctid attend une valeur."
            CTID_FLAG=$2; shift 2 ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "Option inconnue : $1 (voir --help)." ;;
    esac
done

CTID=${CTID_FLAG:-$CTID}
[[ $CTID =~ ^[0-9]+$ ]] || die "CTID invalide : $CTID"

[[ $MODE == status ]] && DRY=1

[[ $EUID -eq 0 ]] || die "A lancer en root sur le noeud Proxmox."
command -v pct >/dev/null || die "pct introuvable : ce script tourne sur l'hote, pas dans le CT."
for f in pg-backup.sh pgbk.sh pg-backup.service pg-backup.timer; do
    [[ -f "$SRC/$f" ]] || die "Depot incomplet : $SRC/$f absent."
done

pct config "$CTID" >/dev/null 2>&1 \
    || die "CT $CTID inexistant. Le conteneur se cree avec le script communautaire (README, section 1)."

# Execute une commande DANS le conteneur.
ct() { pct exec "$CTID" -- "$@"; }

# Execute une commande modifiante, ou l'annonce seulement en --dry-run.
run() {
    if [[ $DRY -eq 1 ]]; then
        printf '  [dry-run] %s\n' "$*"
        return 0
    fi
    "$@"
}

# ------------------------------------------------- A. prerequis conteneur

PROTECTION_ORIG=
restore_protection() {
    # Une interruption au milieu ne doit pas laisser le CT deprotege.
    [[ ${PROTECTION_ORIG:-} == 1 ]] || return 0
    [[ $(pct config "$CTID" | awk -F': *' '/^protection:/ {print $2}') == 1 ]] && return 0
    warn "retablissement de la protection du CT $CTID"
    pct set "$CTID" --protection 1 || err "echec du retablissement de la protection sur $CTID"
}

wait_for() {
    local label=$1 deadline=$((SECONDS + WAIT_TIMEOUT)); shift
    log "  attente : $label"
    while ! "$@" >/dev/null 2>&1; do
        [[ $SECONDS -lt $deadline ]] || die "delai depasse (${WAIT_TIMEOUT}s) : $label"
        sleep 3
    done
}

container_prereqs() {
    log "== Prerequis conteneur (CT $CTID)"

    local cfg mp1_want mp1_have startup need_reboot=0 lowered=0
    cfg=$(pct config "$CTID")
    PROTECTION_ORIG=$(awk -F': *' '/^protection:/ {print $2}' <<<"$cfg")
    trap restore_protection EXIT

    mp1_want="${SRC},mp=${MP},ro=1"
    mp1_have=$(awk -F': *' '/^mp1:/ {print $2}' <<<"$cfg")

    if [[ $mp1_have == "$mp1_want" ]]; then
        log "  mp1 conforme : $mp1_have"
        note OK "mp1 -> $MP"
    else
        [[ -n $mp1_have ]] && warn "mp1 divergent : $mp1_have"
        log "  pose du montage : $mp1_want"
        # La protection interdit toute modification de disque, mp1 compris.
        if [[ ${PROTECTION_ORIG:-0} == 1 ]]; then
            run pct set "$CTID" --protection 0
            lowered=1
        fi
        run pct set "$CTID" --mp1 "$mp1_want"
        need_reboot=1
        note POSE "mp1 -> $MP"
    fi

    startup=$(awk -F': *' '/^startup:/ {print $2}' <<<"$cfg")
    if [[ -n $startup ]]; then
        log "  startup : $startup"
        note OK "startup"
    else
        log "  pose de startup order=1 (PostgreSQL avant ses locataires)"
        run pct set "$CTID" --startup order=1
        note POSE "startup order=1"
    fi

    if [[ $need_reboot -eq 1 ]]; then
        # Un point de montage n'est pris en compte qu'au demarrage, et
        # pct reboot rend la main bien avant que le CT ne soit utilisable.
        log "  redemarrage du CT (un mp n'est lu qu'au demarrage)"
        run pct reboot "$CTID"
        if [[ $DRY -eq 0 ]]; then
            wait_for "CT $CTID en etat running" \
                     bash -c "pct status $CTID | grep -q running"
            wait_for "postgresql actif" ct systemctl is-active --quiet postgresql
        fi
    fi

    if [[ $lowered -eq 1 ]]; then
        run pct set "$CTID" --protection 1
    elif [[ ${PROTECTION_ORIG:-0} != 1 ]]; then
        warn "le CT $CTID n'est pas protege (pct set $CTID --protection 1)"
    fi
    trap - EXIT
}

# ------------------------------------------------------- B. pose dans le CT

CLUSTER_DIR=
detect_cluster() {
    local clusters n
    clusters=$(ct pg_lsclusters -h 2>/dev/null | awk 'NF {print $1" "$2}')
    n=$(grep -c . <<<"$clusters" || true)
    [[ $n -ge 1 ]] || die "aucun cluster PostgreSQL dans le CT $CTID."
    [[ $n -eq 1 ]] || die "plusieurs clusters PostgreSQL, cible ambigue :"$'\n'"$clusters"
    CLUSTER_DIR="/etc/postgresql/$(awk '{print $1"/"$2}' <<<"$clusters")"
    log "  cluster : $(tr ' ' '/' <<<"$clusters")  ($CLUSTER_DIR)"
}

# Pose un symlink et signale s'il a change. 0 = deja conforme, 1 = pose.
link_config() {
    local name=$1 target=$2 have
    have=$(ct readlink -f "$target" 2>/dev/null || true)
    if [[ $have == "$MP/$name" ]]; then
        log "  symlink conforme : $target"
        note OK "$name"
        return 0
    fi
    log "  pose du symlink : $target -> $MP/$name"
    run ct ln -sfn "$MP/$name" "$target"
    note POSE "$name"
    return 1
}

# Copie un fichier dans le CT s'il differe en contenu ou en mode.
# 0 = deja conforme, 1 = copie.
install_ct() {
    local mode=$1 src=$2 dest=$3 label
    label=$(basename "$dest")
    if ct sh -c "cmp -s '$src' '$dest' && [ \"\$(stat -c %a '$dest')\" = '$mode' ]" 2>/dev/null; then
        log "  $dest a jour"
        note OK "$label"
        return 0
    fi
    log "  installation de $dest (mode $mode)"
    run ct install -m "$mode" "$src" "$dest"
    note POSE "$label"
    return 1
}

container_setup() {
    log "== Pose dans le CT $CTID"

    if ! ct test -f "$MP/pg-backup.sh" 2>/dev/null; then
        # En apply, c'est bloquant. En --status / --dry-run sur un CT vierge,
        # le montage n'a pas encore ete pose : on le signale sans abandonner,
        # sinon ces deux modes ne pourraient rien rapporter d'une installation
        # qui n'a pas encore eu lieu.
        [[ $DRY -eq 1 ]] || die "$MP absent dans le CT $CTID. Le montage mp1 n'est lu qu'au demarrage : pct reboot $CTID."
        warn "$MP absent du CT $CTID — pose non evaluable (rejouer sans --dry-run)"
        note KO "$MP absent"
        return 0
    fi

    detect_cluster

    local changed=0
    link_config 10-homelab.conf "$CLUSTER_DIR/conf.d/10-homelab.conf" || changed=1
    link_config pg_hba.conf     "$CLUSTER_DIR/pg_hba.conf"            || changed=1

    # install et non ln : le montage est en lecture seule et ne peut pas
    # porter le bit d'execution.
    local copied=0
    install_ct 644 "$MP/pg-backup.service" /etc/systemd/system/pg-backup.service || copied=1
    install_ct 644 "$MP/pg-backup.timer"   /etc/systemd/system/pg-backup.timer   || copied=1
    install_ct 755 "$MP/pg-backup.sh"      /usr/local/bin/pg-backup.sh           || copied=1
    install_ct 755 "$MP/pgbk.sh"           /usr/local/bin/pgbk                   || copied=1

    # daemon-reload n'a de sens que si une unite a bouge.
    [[ $copied -eq 1 ]] && run ct systemctl daemon-reload

    if ct systemctl is-enabled --quiet pg-backup.timer 2>/dev/null; then
        log "  pg-backup.timer deja active"
        note OK "pg-backup.timer (active)"
    else
        log "  activation de pg-backup.timer"
        run ct systemctl enable --now pg-backup.timer
        note POSE "pg-backup.timer (active)"
    fi

    # listen_addresses exige un restart : un reload ne suffit pas a la
    # premiere pose, quand pg_hba et le drop-in viennent d'apparaitre.
    #
    # Le reload, lui, est inconditionnel et c'est voulu : les fichiers de
    # configuration etant des symlinks vers le depot, leur contenu a pu changer
    # avec le git pull sans que rien ici ne puisse s'en apercevoir. Un reload
    # est sans effet de bord, l'economiser ferait manquer un pg_hba modifie.
    if [[ $changed -eq 1 || $FORCE_RESTART -eq 1 ]]; then
        log "  restart de postgresql (configuration modifiee)"
        run ct systemctl restart postgresql
    else
        log "  reload de postgresql"
        run ct systemctl reload postgresql
    fi
}

# ------------------------------------------------------------ C. controles

checks() {
    log "== Controles"

    # Un reload reussi ne prouve pas que le fichier a ete relu : PostgreSQL
    # garde l'ancienne configuration en memoire si la nouvelle est invalide.
    local rules bad
    rules=$(ct sudo -u postgres psql -tAF'|' -c \
        "SELECT line_number, type, database, user_name, address, auth_method, coalesce(error,'') FROM pg_hba_file_rules ORDER BY line_number" \
        2>/dev/null || true)
    if [[ -z $rules ]]; then
        warn "pg_hba_file_rules illisible : PostgreSQL repond-il ?"
        note KO "pg_hba"
    else
        sed 's/^/    /' <<<"$rules"
        # Compte fait par le moteur : un awk positionnel se decalerait au
        # moindre changement de colonnes de pg_hba_file_rules.
        bad=$(ct sudo -u postgres psql -tAc \
            "SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL" 2>/dev/null || echo "?")
        if [[ $bad == 0 ]]; then
            log "  pg_hba : $(grep -c . <<<"$rules") regle(s), aucune erreur"
            note OK "pg_hba"
        else
            warn "$bad regle(s) pg_hba en erreur — voir la derniere colonne"
            note KO "pg_hba ($bad regle(s) en erreur)"
        fi
    fi

    # Deux sockets attendus. Un seul, sur la boucle locale, est le symptome
    # de la panne listen_addresses documentee : service actif, base injoignable.
    local socks n
    socks=$(ct ss -lntp 2>/dev/null | grep ':5432' || true)
    n=$(grep -c . <<<"${socks:-}" || true)
    sed 's/^/    /' <<<"${socks:-}"
    if [[ $n -ge 2 ]]; then
        log "  ecoute : $n socket(s)"
        note OK "listen 5432"
    else
        warn "$n socket sur 5432 — deux attendus (0.0.0.0 et [::]), voir docs/postgresql-listen-addresses-lxc.md"
        note KO "listen 5432"
    fi

    if ct systemctl is-enabled --quiet pg-backup.timer 2>/dev/null; then
        ct systemctl list-timers pg-backup.timer --no-pager 2>/dev/null | sed 's/^/    /'
        note OK "pg-backup.timer (verifie)"
    else
        warn "pg-backup.timer n'est pas active"
        note KO "pg-backup.timer (inactive)"
    fi
}

# ------------------------------------------------ D. pgbk sur l'hote

write_conf() {
    log "== CTID consigne"
    # Source unique du CTID. Sans ce fichier, pgbk s'arrete
    # plutot que de taper dans un conteneur suppose.
    local want
    want=$(printf '%s\n' \
        "# Genere par pg-deploy.sh — conteneur PostgreSQL pilote par pgbk." \
        "# Changer de CT : rejouer pg-deploy.sh --ctid <ID>, pas editer ce fichier." \
        "PG_CTID=$CTID")
    if [[ -r $CONF ]] && [[ $(cat "$CONF") == "$want" ]]; then
        log "  $CONF : PG_CTID=$CTID"
        note OK "$CONF (PG_CTID=$CTID)"
    else
        log "  ecriture de $CONF (PG_CTID=$CTID)"
        if [[ $DRY -eq 1 ]]; then
            printf '  [dry-run] ecrire %s avec PG_CTID=%s\n' "$CONF" "$CTID"
        else
            printf '%s\n' "$want" > "$CONF"
            chmod 644 "$CONF"
        fi
        note POSE "$CONF (PG_CTID=$CTID)"
    fi
    log
}

host_wrapper() {
    log "== pgbk sur l'hote"
    # Exactement le meme fichier que dans le CT : pgbk.sh se comporte selon
    # l'endroit ou il tourne. Un seul contenu, donc rien a confondre.
    if cmp -s "$SRC/pgbk.sh" "$HOST_PGBK" 2>/dev/null \
       && [[ $(stat -c %a "$HOST_PGBK" 2>/dev/null) == 755 ]]; then
        log "  $HOST_PGBK a jour"
        note OK "$HOST_PGBK"
    else
        log "  installation de $HOST_PGBK"
        run install -m 755 "$SRC/pgbk.sh" "$HOST_PGBK"
        note POSE "$HOST_PGBK"
    fi
    log
}

# ---------------------------------------------------------------- execution

log "CT $CTID — depot $SRC"
[[ $MODE == status ]] && log "(mode --status : aucune modification)"
log

if [[ $DO_CONTAINER -eq 1 ]]; then
    container_prereqs
    log
fi

container_setup
log
write_conf
host_wrapper
log
checks
log

log "== Resume"
printf '%s\n' "${SUMMARY[@]}"

if [[ $DRY -eq 1 ]]; then
    log
    log "Aucune modification appliquee."
fi
