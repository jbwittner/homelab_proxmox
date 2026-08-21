#!/bin/bash
#
# Deploie le CT PostgreSQL mutualise depuis l'hote Proxmox : point de montage
# du depot, symlinks de configuration, unites systemd de sauvegarde, pgbk sur
# l'hote et dans le conteneur, et la copie hors-site vers GCS sur l'hote.
#
# DEUX MACHINES, UN SEUL SCRIPT, DEUX REPERTOIRES. ct/ est la charge utile du
# point de montage : c'est LUI, et lui seul, qui est monte en /etc/pgsql-git.
# host/ porte ce qui s'installe sur le noeud (pgbk-offsite.*) et que le
# conteneur n'a aucune raison de voir — a commencer par le nom du bucket et le
# chemin de la cle GCS. Le critere n'est pas « quelle machine l'execute » mais
# « est-ce la charge utile du mp1 » : pgbk.sh tourne des deux cotes et vit dans
# ct/, l'hote le lit a travers la frontiere.
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
# TOUT PASSE PAR CE SCRIPT. Il installe les paquets manquants, pose les points
# de montage (dont le dataset de sauvegarde mp2), la configuration, les scripts,
# les unites systemd, la configuration rclone, et declenche la premiere
# sauvegarde puis la premiere copie hors-site. Le README liste les gestes
# courants, doc/RUNBOOK.md decrit en detail ce que fait ce script.
#
# Deux choses restent hors de sa portee, et c'est voulu :
#   - la CREATION du conteneur, qui appartient au script communautaire ;
#   - la CLE du compte de service GCP, qui est un secret et n'a rien a faire
#     dans le depot. Le script dit ou la deposer et n'arme pas le hors-site
#     tant qu'elle manque.
#
# Les operations qui GENERENT UN SECRET ne sont pas jouees par defaut : un
# deploiement de routine ne doit pas faire apparaitre un mot de passe dans un
# terminal ni en creer un dont personne n'attend la rotation. Elles sont
# derriere --admin et --tenant.
#
# Usage :
#   ./pg-deploy.sh                deploiement complet (pose ou mise a jour)
#   ./pg-deploy.sh --status       etat de chaque element, ne change rien
#   ./pg-deploy.sh --dry-run      affiche ce qui serait fait
#   ./pg-deploy.sh --ctid 201     cible un autre conteneur, et le consigne
#   ./pg-deploy.sh --restart      force un restart de postgresql
#   ./pg-deploy.sh --no-container saute les prerequis conteneur (mp1, protection)
#   ./pg-deploy.sh --no-offsite   saute la copie hors-site GCS (section F)
#   ./pg-deploy.sh --no-install   n'installe aucun paquet (noeud sans reseau)
#   ./pg-deploy.sh --no-first-run ne declenche ni sauvegarde ni copie initiale
#   ./pg-deploy.sh --admin jbwittner   cree le compte d'administration s'il manque
#   ./pg-deploy.sh --tenant forgejo    cree un locataire (base + role) s'il manque
#
# A lancer en root sur le noeud Proxmox, pas dans le CT.

set -euo pipefail

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)   # racine du service
CT_SRC="$SRC/ct"                                     # source reelle du mp1
HOST_SRC="$SRC/host"                                 # ce qui s'installe sur le noeud
MP=/etc/pgsql-git                                    # cible du montage dans le CT
HOST_PGBK=/usr/local/sbin/pgbk
HOST_PG=/usr/local/sbin/pg                           # point d'entree unique
HOST_LIB=/usr/local/lib/pgtool                       # arbre d'import de pg
CT_LIB=/usr/local/lib/pgtool                         # le meme, dans le CT
CT_PG=/usr/local/bin/pg                              # point d'entree du CT
LIB_SRC="$(cd "$SRC/../.." && pwd)/lib"              # briques partagees du depot
HOST_OFFSITE=/usr/local/bin/pgbk-offsite             # copie hors-site, cote HOTE
OFFSITE_UNIT=/etc/systemd/system/pgbk-offsite.service
OFFSITE_DROPIN_DIR="${OFFSITE_UNIT}.d"
OFFSITE_DROPIN="${OFFSITE_DROPIN_DIR}/10-noeud.conf"

# Dataset des sauvegardes (mp2). Volume distinct du disque systeme du CT :
# un incident sur le SSD qui porte PGDATA ne doit pas emporter les dumps.
# backup=0 tient les vzdump du CT a l'ecart de 50 Go de dumps.
MP2_MOUNT=${PG_MP2_MOUNT:-/var/backups/postgresql}   # vue CT
MP2_STORAGE=${PG_MP2_STORAGE:-data}                  # pool Proxmox (NVMe 1 To)
MP2_SIZE=${PG_MP2_SIZE:-50}                          # Go, a la creation seulement
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
DO_OFFSITE=1
DO_INSTALL=1
DO_FIRST_RUN=1
ADMIN_ROLE=
TENANT_NAME=

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
        --no-offsite)   DO_OFFSITE=0; shift ;;
        --no-install)   DO_INSTALL=0; shift ;;
        --no-first-run) DO_FIRST_RUN=0; shift ;;
        --admin)
            [[ $# -ge 2 ]] || die "--admin attend un nom de role."
            ADMIN_ROLE=$2; shift 2 ;;
        --tenant)
            [[ $# -ge 2 ]] || die "--tenant attend un nom de locataire."
            TENANT_NAME=$2; shift 2 ;;
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
    [[ -f "$CT_SRC/$f" ]] || die "Depot incomplet : $CT_SRC/$f absent."
done
[[ -x "$SRC/pg" ]]            || die "Depot incomplet : $SRC/pg absent ou non executable."
[[ -f "$SRC/pgtool/cli.py" ]] || die "Depot incomplet : $SRC/pgtool/cli.py absent."
[[ -d "$LIB_SRC/core" ]]      || die "Depot incomplet : $LIB_SRC/core absent."
[[ -d "$LIB_SRC/proxmox" ]]   || die "Depot incomplet : $LIB_SRC/proxmox absent."
if [[ $DO_OFFSITE -eq 1 ]]; then
    for f in pgbk-offsite.sh pgbk-offsite.service pgbk-offsite.timer; do
        [[ -f "$HOST_SRC/$f" ]] || die "Depot incomplet : $HOST_SRC/$f absent."
    done
fi

pct config "$CTID" >/dev/null 2>&1 \
    || die "CT $CTID inexistant. Le conteneur se cree avec le script communautaire (doc/RUNBOOK.md, section 1)."

# Execute une commande DANS le conteneur.
ct() { pct exec "$CTID" -- "$@"; }

# Valeur d'une cle de « pct config ». Volontairement en sed et non en
# « awk -F': *' » : la valeur d'un point de montage contient elle-meme un
# deux-points (data:subvol-200-disk-0), qu'un separateur ': *' couperait en
# plein milieu — la cle serait lue comme « data » et toutes les comparaisons
# qui suivent deviendraient fausses.
cfg_get() { sed -n "s/^$2:[[:space:]]*//p" <<<"$1"; }

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
MP2_STATE=inconnu   # ok | divergent | inconnu (section A non jouee)
restore_protection() {
    # Une interruption au milieu ne doit pas laisser le CT deprotege.
    [[ ${PROTECTION_ORIG:-} == 1 ]] || return 0
    [[ $(cfg_get "$(pct config "$CTID")" protection) == 1 ]] && return 0
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

# Leve la protection du CT, une seule fois, avant toute modification de disque.
# Ecrit « lowered » dans la portee de container_prereqs : bash donne aux
# fonctions appelees l'acces aux locales de l'appelante, et c'est bien ce qu'on
# veut ici — une seule bascule, remontee une seule fois en fin de section.
need_unprotect() {
    [[ ${PROTECTION_ORIG:-0} == 1 && $lowered -eq 0 ]] || return 0
    log "  levee temporaire de la protection du CT $CTID"
    run pct set "$CTID" --protection 0
    lowered=1
}

container_prereqs() {
    log "== Prerequis conteneur (CT $CTID)"

    local cfg mp1_want mp1_have mp2_have feat startup need_reboot=0 lowered=0

    # Tout le reste de ce script parle au CT : il doit tourner. Un conteneur
    # a l'arret n'est pas une erreur, c'est juste une etape de plus.
    if [[ $(pct status "$CTID" | awk '{print $2}') != running ]]; then
        log "  CT $CTID a l'arret — demarrage"
        run pct start "$CTID"
        if [[ $DRY -eq 0 ]]; then
            wait_for "CT $CTID en etat running" \
                     bash -c "pct status $CTID | grep -q running"
            wait_for "postgresql actif" ct systemctl is-active --quiet postgresql
        fi
        note POSE "CT $CTID (demarre)"
    fi

    cfg=$(pct config "$CTID")
    PROTECTION_ORIG=$(cfg_get "$cfg" protection)
    trap restore_protection EXIT

    # nesting=1 est OBLIGATOIRE sur Debian 13 : sans lui, les unites qui
    # utilisent PrivateTmp ou NoNewPrivileges — pg-backup.service en fait
    # partie — echouent en 243/CREDENTIALS. Le conteneur demarre quand meme,
    # en etat degrade, et la sauvegarde ne part jamais.
    feat=$(cfg_get "$cfg" features)
    if [[ $feat == *nesting=1* ]]; then
        log "  features : $feat"
        note OK "nesting"
    else
        local feat_want="nesting=1"
        [[ -n $feat ]] && feat_want="$(sed 's/nesting=0//; s/,,/,/g; s/^,//; s/,$//' <<<"$feat"),nesting=1"
        warn "nesting absent des features : ${feat:-(aucune)}"
        log "  pose de features=$feat_want"
        need_unprotect
        run pct set "$CTID" --features "$feat_want"
        need_reboot=1
        note POSE "nesting"
    fi

    mp1_want="${CT_SRC},mp=${MP},ro=1"
    mp1_have=$(cfg_get "$cfg" mp1)

    if [[ $mp1_have == "$mp1_want" ]]; then
        log "  mp1 conforme : $mp1_have"
        note OK "mp1 -> $MP"
    else
        [[ -n $mp1_have ]] && warn "mp1 divergent : $mp1_have"
        log "  pose du montage : $mp1_want"
        # La protection interdit toute modification de disque, mp1 compris.
        need_unprotect
        run pct set "$CTID" --mp1 "$mp1_want"
        need_reboot=1
        note POSE "mp1 -> $MP"
    fi

    # mp2 : le volume des sauvegardes, sur un DISQUE PHYSIQUE DISTINCT de
    # celui de PGDATA. C'est toute la raison d'etre d'un second point de
    # montage — une panne du SSD qui porte la base ne doit pas emporter les
    # dumps avec elle.
    mp2_have=$(cfg_get "$cfg" mp2)
    if [[ -z $mp2_have ]]; then
        log "  creation du volume de sauvegarde : ${MP2_STORAGE}:${MP2_SIZE} -> ${MP2_MOUNT}"
        need_unprotect
        # « storage:taille » demande a Proxmox d'allouer le volume ; il
        # apparait ensuite dans la config sous son vrai nom (subvol-<CTID>-...).
        run pct set "$CTID" --mp2 "${MP2_STORAGE}:${MP2_SIZE},mp=${MP2_MOUNT},backup=0"
        MP2_STATE=ok
        need_reboot=1
        note POSE "mp2 -> $MP2_MOUNT (${MP2_SIZE} Go)"
    elif [[ $mp2_have == *"mp=${MP2_MOUNT}"* ]]; then
        MP2_STATE=ok
        log "  mp2 conforme : $mp2_have"
        [[ $mp2_have == *backup=0* ]] \
            || warn "mp2 sans backup=0 : les vzdump du CT embarquent ${MP2_SIZE} Go de dumps"
        note OK "mp2 -> $MP2_MOUNT"
    else
        # Ne pas toucher : un mp2 existant qui pointe ailleurs porte peut-etre
        # des donnees. Le dire, laisser l'humain trancher.
        warn "mp2 present mais monte ailleurs : $mp2_have"
        warn "  attendu mp=${MP2_MOUNT} — corriger a la main avant de continuer"
        MP2_STATE=divergent
        note KO "mp2 (divergent)"
    fi

    startup=$(cfg_get "$cfg" startup)
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
        log "  retablissement de la protection du CT $CTID"
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

# Dependances du CT. L'image du script communautaire les porte deja ; rien ne
# le garantit sur un conteneur recree autrement, et l'absence ne se voit qu'au
# moment ou une sauvegarde echoue.
container_packages() {
    # pgbk et pg-backup.sh passent tous les deux par « sudo -u postgres ».
    if ct sh -c 'command -v sudo >/dev/null 2>&1'; then
        log "  sudo present"
        note OK "sudo (CT)"
    elif [[ $DO_INSTALL -eq 0 ]]; then
        warn "sudo absent du CT et --no-install : pgbk ne fonctionnera pas"
        note KO "sudo (CT, absent)"
    else
        log "  installation de sudo dans le CT"
        run ct apt-get update -qq
        run ct env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sudo
        note POSE "sudo (CT)"
    fi

    # python3 vient du template Debian, pas d'une decision explicite : le
    # moteur en depend, autant le constater ici que le decouvrir au moment
    # d'une restauration.
    if ct test -x /usr/bin/python3 2>/dev/null; then
        log "  python3 present (CT)"
        note OK "python3 (CT)"
    elif [[ $DO_INSTALL -eq 0 ]]; then
        warn "python3 absent du CT et --no-install : le moteur pg ne sera pas pose"
        note KO "python3 (CT, absent)"
    else
        log "  installation de python3 dans le CT"
        run ct apt-get update -qq
        run ct env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-minimal
        note POSE "python3 (CT)"
    fi

    # LVM-thin : sans fstrim, les blocs liberes ne sont jamais rendus au pool,
    # qui est surprovisionne. Un pool sature arrete net le serveur.
    if ct systemctl is-enabled --quiet fstrim.timer 2>/dev/null; then
        log "  fstrim.timer actif"
        note OK "fstrim.timer (CT)"
    else
        log "  activation de fstrim.timer dans le CT"
        run ct systemctl enable --now fstrim.timer
        note POSE "fstrim.timer (CT)"
    fi
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

    container_packages
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

# Arbre d'import DANS le conteneur. Seuls core et pgtool y vont : proxmox ne
# quitte JAMAIS l'hote, et un conteneur n'a rien a faire avec pct.
CT_PY_TREE=("$LIB_SRC/core" "$SRC/pgtool")

ct_py_manifest() {
    local src f
    for src in "${CT_PY_TREE[@]}"; do
        while IFS= read -r f; do
            printf '%s/%s\t%s\n' "$(basename "$src")" "${f#"$src"/}" "$f"
        done < <(find "$src" -type f -name '*.py' | sort)
    done
}

# Empreintes des fichiers deja presents dans le CT, en UN aller-retour plutot
# qu'un « pct exec cmp » par fichier.
ct_digests() {
    # Ne retenir QUE des lignes « <64 hex><2 espaces><chemin> ». Sans ce filtre,
    # n'importe quelle sortie inattendue du conteneur — un avertissement, une
    # ligne de shell — serait lue comme un fichier present, donc comme un
    # fichier a RETIRER a l'etape d'elagage. Constate au banc d'essai, qui
    # produisait « rm -f /usr/local/lib/pgtool/1 ».
    ct sh -c 'cd "$1" 2>/dev/null && find . -type f -exec sha256sum {} + || true' \
       sh "$CT_LIB" 2>/dev/null \
      | sed -n 's#^\([0-9a-f]\{64\}\)  \./#\1  #p'
}

container_pgtool() {
    log "== Moteur Python du CT $CTID"

    if ! ct test -x /usr/bin/python3 2>/dev/null; then
        warn "python3 absent du CT $CTID — moteur non pose, pgbk reste en place"
        note KO "$CT_LIB (python3 absent)"
        return 0
    fi

    # POUSSE, PAS MONTE. Le bind-mount ne couvre que ct/, et l'y ajouter
    # exposerait le moteur a un « git pull » en cours : un arbre d'import a
    # moitie a jour donne un ImportError au pire moment. « pct push » depose
    # une copie figee jusqu'au prochain deploiement.
    local changed=0 rel src empreinte
    local want; want=$(mktemp)
    local have; have=$(mktemp)
    ct_digests > "$have" || true

    while IFS=$'\t' read -r rel src; do
        printf '%s\n' "$rel" >> "$want"
        empreinte=$(sha256sum "$src" | cut -d' ' -f1)
        grep -qxF "$empreinte  $rel" "$have" || changed=1
    done < <(ct_py_manifest)

    # Ce que le depot ne contient plus doit partir : un module renomme
    # laisserait son ancetre, qui continuerait de s'importer.
    local -a extra=()
    while IFS= read -r rel; do
        [[ -n $rel ]] || continue
        grep -qxF "$rel" "$want" || extra+=("$rel")
    done < <(cut -d' ' -f3- "$have")
    [[ ${#extra[@]} -gt 0 ]] && changed=1

    if [[ $changed -eq 0 ]]; then
        log "  $CT_LIB a jour dans le CT"
        note OK "$CT_LIB (CT)"
    else
        log "  depot de $CT_LIB dans le CT"
        while IFS=$'\t' read -r rel src; do
            run ct mkdir -p "$CT_LIB/$(dirname "$rel")"
            run pct push "$CTID" "$src" "$CT_LIB/$rel" --perms 0644
        done < <(ct_py_manifest)
        for rel in "${extra[@]}"; do
            log "  retrait de $rel (absent du depot)"
            run ct rm -f "$CT_LIB/$rel"
        done
        note POSE "$CT_LIB (CT)"
    fi
    rm -f "$want" "$have"

    # Le lanceur, seul fichier executable de l'ensemble. Compare par empreinte
    # et non par redirection : « pct exec » n'est pas un tube ordinaire.
    local pg_local pg_distant
    pg_local=$(sha256sum "$SRC/pg" | cut -d' ' -f1)
    pg_distant=$(ct sha256sum "$CT_PG" 2>/dev/null | cut -d' ' -f1 || true)
    if [[ $pg_local == "${pg_distant:-}" ]]; then
        log "  $CT_PG a jour"
        note OK "$CT_PG (CT)"
    else
        log "  pose de $CT_PG"
        run pct push "$CTID" "$SRC/pg" "$CT_PG" --perms 0755
        note POSE "$CT_PG (CT)"
    fi
    log
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
        warn "$n socket sur 5432 — deux attendus (0.0.0.0 et [::]), voir doc/RUNBOOK.md section 4"
        note KO "listen 5432"
    fi

    if ct systemctl is-enabled --quiet pg-backup.timer 2>/dev/null; then
        ct systemctl list-timers pg-backup.timer --no-pager 2>/dev/null | sed 's/^/    /'
        note OK "pg-backup.timer (verifie)"
    else
        warn "pg-backup.timer n'est pas active"
        note KO "pg-backup.timer (inactive)"
    fi

    # Timer de l'HOTE, pas du CT : pas de ct() ici.
    if [[ $DO_OFFSITE -eq 1 ]] && systemctl is-enabled --quiet pgbk-offsite.timer 2>/dev/null; then
        systemctl list-timers pgbk-offsite.timer --no-pager 2>/dev/null | sed 's/^/    /'
        note OK "pgbk-offsite.timer (verifie)"
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

# Pendant hote de install_ct : copie si le contenu ou le mode differe.
# 0 = deja conforme, 1 = copie.
install_host() {
    local mode=$1 src=$2 dest=$3
    if cmp -s "$src" "$dest" 2>/dev/null \
       && [[ $(stat -c %a "$dest" 2>/dev/null) == "$mode" ]]; then
        log "  $dest a jour"
        note OK "$dest"
        return 0
    fi
    log "  installation de $dest (mode $mode)"
    run install -m "$mode" "$src" "$dest"
    note POSE "$dest"
    return 1
}

host_wrapper() {
    log "== pgbk sur l'hote"
    # Exactement le meme fichier que dans le CT : pgbk.sh se comporte selon
    # l'endroit ou il tourne. Un seul contenu, donc rien a confondre.
    # Lu dans ct/ : pgbk.sh est la charge utile du montage ET le point d'entree
    # de l'hote. Un seul fichier, deux roles — la frontiere ct/ est une
    # frontiere de VISIBILITE, pas d'execution.
    install_host 755 "$CT_SRC/pgbk.sh" "$HOST_PGBK" || true
    log
}

# Arbre d'import de « pg ». Les trois paquets sont deposes cote a cote, ce qui
# reproduit la disposition qu'aura le conteneur — lui n'en recevra que deux,
# proxmox ne quittant jamais l'hote.
PY_TREE=("$LIB_SRC/core" "$LIB_SRC/proxmox" "$SRC/pgtool")

# Liste « chemin relatif <TAB> source » de tout ce qui doit etre pose.
py_manifest() {
    local src f
    for src in "${PY_TREE[@]}"; do
        while IFS= read -r f; do
            printf '%s/%s\t%s\n' "$(basename "$src")" "${f#"$src"/}" "$f"
        done < <(find "$src" -type f -name '*.py' | sort)
    done
}

host_pgtool() {
    log "== Outillage Python de l'hote"

    # python3 vient du template Debian, pas d'une decision explicite. Le
    # constater, ne pas le supposer : « pg » refuse de demarrer en dessous de
    # 3.11, et mieux vaut l'apprendre ici qu'a 3h30.
    local py=/usr/bin/python3 pyver
    if [[ ! -x $py ]]; then
        warn "python3 absent de $py — pg ne peut pas tourner"
        note KO "python3 (absent)"
        return 0
    fi
    pyver=$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo '?')
    if ! "$py" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        warn "python3 $pyver sur $py — 3.11 minimum requis par pg"
        note KO "python3 ($pyver)"
        return 0
    fi
    log "  python3 $pyver ($py)"
    note OK "python3 $pyver"

    # COPIE ET NON SYMLINK, comme pour les scripts : le depot peut etre en
    # cours de « git pull » a l'heure ou un timer se declenche, et un arbre
    # d'import a moitie a jour donne un ImportError au pire moment.
    local changed=0 rel src dest
    local want; want=$(mktemp)
    while IFS=$'\t' read -r rel src; do
        printf '%s\n' "$rel" >> "$want"
        cmp -s "$src" "$HOST_LIB/$rel" 2>/dev/null || changed=1
    done < <(py_manifest)

    # Ce que le depot ne contient plus doit partir : un module renomme
    # laisserait son ancetre en place, et cet ancetre continuerait de
    # s'importer. Le noeud tournerait sur du code absent du depot.
    local -a extra=()
    if [[ -d $HOST_LIB ]]; then
        while IFS= read -r dest; do
            rel="${dest#"$HOST_LIB"/}"
            grep -qxF "$rel" "$want" || extra+=("$rel")
        done < <(find "$HOST_LIB" -type f -name '*.py' | sort)
    fi
    [[ ${#extra[@]} -gt 0 ]] && changed=1

    if [[ $changed -eq 0 ]]; then
        log "  $HOST_LIB a jour"
        note OK "$HOST_LIB"
    else
        log "  pose de $HOST_LIB"
        while IFS=$'\t' read -r rel src; do
            run install -D -m 644 "$src" "$HOST_LIB/$rel"
        done < <(py_manifest)
        for rel in "${extra[@]}"; do
            log "  retrait de $rel (absent du depot)"
            run rm -f "$HOST_LIB/$rel"
        done
        note POSE "$HOST_LIB"
    fi
    rm -f "$want"

    # Le lanceur : seul fichier executable de l'ensemble, et le seul du PATH.
    install_host 755 "$SRC/pg" "$HOST_PG" || true
    log
}

# ------------------------------------- E. dependances de l'hote (paquets, GCS)

host_packages() {
    # Appelee seulement quand le hors-site est demande : rclone n'est une
    # dependance que de lui.
    log "== Paquets de l'hote"
    local rclone_bin
    rclone_bin=$(unit_env PGBK_OFFSITE_RCLONE /usr/bin/rclone)

    if [[ -x $rclone_bin ]]; then
        log "  rclone $("$rclone_bin" version 2>/dev/null | awk 'NR==1 {print $2}') ($rclone_bin)"
        note OK "rclone"
    elif [[ $DO_INSTALL -eq 0 ]]; then
        warn "rclone absent et --no-install : la copie hors-site ne sera pas armee"
        note KO "rclone (absent)"
    else
        log "  installation de rclone"
        run apt-get update -qq
        run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rclone
        # Le paquet trixie s'installe en /usr/bin/rclone. Si l'unite pointe
        # ailleurs, mieux vaut le dire ici que d'echouer a 3h30.
        if [[ $DRY -eq 0 && ! -x $rclone_bin ]]; then
            die "rclone installe mais absent de $rclone_bin — corriger PGBK_OFFSITE_RCLONE dans pgbk-offsite.service"
        fi
        note POSE "rclone"
    fi
    log
}

rclone_config() {
    log "== Configuration rclone (hote)"
    local conf key remote

    conf=$(unit_env PGBK_OFFSITE_CONFIG /root/.config/rclone/rclone.conf)
    key=$(unit_env PGBK_OFFSITE_KEY /root/.config/rclone/pgsql-backups.json)
    remote=$(unit_env PGBK_OFFSITE_REMOTE gcs)

    run install -d -m 700 "$(dirname "$conf")"

    if [[ ! -f $conf ]]; then
        log "  ecriture de $conf (remote [$remote])"
        if [[ $DRY -eq 1 ]]; then
            printf '  [dry-run] ecrire %s\n' "$conf"
        else
            # bucket_policy_only : le bucket est en acces uniforme (UBLA), qui
            # refuse les ACL par objet. Sans cette ligne, rclone joint une ACL
            # heritee a chaque insertion et GCS rejette TOUT en 400.
            printf '%s\n' \
                "# Genere par pg-deploy.sh — remote de la copie hors-site." \
                "[$remote]" \
                "type = google cloud storage" \
                "service_account_file = $key" \
                "bucket_policy_only = true" > "$conf"
            chmod 600 "$conf"
        fi
        note POSE "$conf"
    else
        # Ne pas reecrire : ce fichier peut porter d'autres remotes, et il est
        # hors depot. On signale ce qui manque avec la ligne exacte a ajouter.
        log "  $conf present"
        note OK "$conf"
        if ! grep -q '^[[:space:]]*bucket_policy_only' "$conf"; then
            warn "bucket_policy_only absent de $conf"
            warn "  sans lui, UBLA refuse chaque insertion en 400 (doc/RUNBOOK.md section 10) :"
            warn "  echo 'bucket_policy_only = true' >> $conf"
        fi
    fi

    # La cle est le SEUL element que ce script ne peut pas poser : un secret
    # n'entre pas dans le depot. Ce qu'il peut faire, c'est en corriger le mode
    # et refuser d'armer le timer tant qu'elle manque (section F).
    if [[ -s $key ]]; then
        if [[ $(stat -c %a "$key" 2>/dev/null) != 600 ]]; then
            log "  chmod 600 $key"
            run chmod 600 "$key"
        fi
        log "  cle du compte de service : $key"
        note OK "cle GCP"
    else
        warn "$key absente"
        warn "  cle du compte de service, a deposer depuis OpenBao — puis rejouer ce script"
        note KO "cle GCP (absente)"
    fi
    log
}

# --------------------------------------------- F. copie hors-site (sur l'HOTE)

# Vue HOTE du dataset de sauvegarde, DEMANDEE A PROXMOX plutot que devinee.
# C'est ce qui rend le hors-site correct sur n'importe quel CTID et n'importe
# quel pool : le chemin suit la configuration reelle du conteneur.
backup_dir_host() {
    local spec volid path
    spec=$(cfg_get "$(pct config "$CTID")" mp2)
    [[ -n $spec ]] || return 1
    volid=${spec%%,*}
    path=$(pvesm path "$volid" 2>/dev/null) || return 1
    [[ -n $path && -d $path ]] || return 1
    printf '%s\n' "$path"
}

# Valeur d'une variable declaree dans pgbk-offsite.service, ou $2 a defaut.
# L'unite est la source unique de verite des chemins du hors-site ; ce script
# les relit plutot que de les redeclarer, ce qui divergerait a la premiere
# modification. Le defaut evite qu'une ligne Environment retiree ne produise un
# controle sur une chaine vide, qui passerait pour un fichier absent.
unit_env() {
    local v
    v=$(awk -F= -v k="$1" '$1 == "Environment" && $2 == k {v=$3} END {print v}' \
        "$HOST_SRC/pgbk-offsite.service")
    printf '%s\n' "${v:-$2}"
}

# Drop-in de l'unite : CE noeud-ci et CE conteneur-ci. L'unite du depot ne
# porte que des valeurs par defaut lisibles ; ce fichier fait autorite. C'est
# lui qui rend le hors-site juste sur --ctid 201 comme sur vert-ysera, sans
# editer quoi que ce soit dans le depot.
# 0 = deja conforme, 1 = ecrit.
write_dropin() {
    local src=$1 node want have
    node=$(hostname -s)
    want=$(printf '%s\n' \
        "# Genere par pg-deploy.sh — ne pas editer, il sera reecrit." \
        "[Service]" \
        "Environment=PGBK_OFFSITE_NODE=$node" \
        "Environment=PGBK_OFFSITE_SRC=$src")
    have=$(cat "$OFFSITE_DROPIN" 2>/dev/null || true)

    if [[ $have == "$want" ]]; then
        log "  $OFFSITE_DROPIN a jour (noeud $node, source $src)"
        note OK "$OFFSITE_DROPIN"
        return 0
    fi
    log "  ecriture de $OFFSITE_DROPIN (noeud $node, source $src)"
    if [[ $DRY -eq 1 ]]; then
        printf '  [dry-run] ecrire %s\n' "$OFFSITE_DROPIN"
    else
        install -d -m 755 "$OFFSITE_DROPIN_DIR"
        printf '%s\n' "$want" > "$OFFSITE_DROPIN"
        chmod 644 "$OFFSITE_DROPIN"
    fi
    note POSE "$OFFSITE_DROPIN"
    return 1
}

host_offsite() {
    log "== Copie hors-site vers GCS (sur l'hote)"

    local rclone_bin key src_dir resolved copied=0 ready=1 armed=0
    rclone_bin=$(unit_env PGBK_OFFSITE_RCLONE /usr/bin/rclone)
    key=$(unit_env PGBK_OFFSITE_KEY /root/.config/rclone/pgsql-backups.json)

    # Le script et les unites sont poses dans tous les cas : ce sont des
    # fichiers inertes tant que le timer n'est pas actif, et les avoir en
    # place permet un « pgbk-offsite --dry-run » de diagnostic.
    install_host 755 "$HOST_SRC/pgbk-offsite.sh"      "$HOST_OFFSITE"                          || copied=1
    install_host 644 "$HOST_SRC/pgbk-offsite.service" "$OFFSITE_UNIT"                          || copied=1
    install_host 644 "$HOST_SRC/pgbk-offsite.timer"   /etc/systemd/system/pgbk-offsite.timer   || copied=1

    if resolved=$(backup_dir_host); then
        src_dir=$resolved
        write_dropin "$resolved" || copied=1
        log "  source : $src_dir (vue hote de $MP2_MOUNT)"
        note OK "pgbk-offsite (source $src_dir)"
    else
        # mp2 pas encore visible : CT jamais redemarre depuis sa creation, ou
        # pool indisponible. On se rabat sur la valeur de l'unite et on
        # verifie au moins qu'elle parle bien de CE conteneur.
        src_dir=$(unit_env PGBK_OFFSITE_SRC "/data/subvol-${CTID}-disk-0")
        warn "volume mp2 non resolu (pvesm) — repli sur l'unite : $src_dir"
        if [[ $src_dir != *subvol-$CTID-* ]]; then
            # Ne pas armer : une copie qui part chaque nuit, verte, sur les
            # sauvegardes d'un autre conteneur est pire qu'une copie absente.
            warn "  et cette valeur ne mentionne pas le CT $CTID"
            note KO "pgbk-offsite (source hors CT $CTID)"
            ready=0
        else
            note KO "pgbk-offsite (source $src_dir non resolue)"
        fi
    fi

    [[ $copied -eq 1 ]] && run systemctl daemon-reload

    # Les identifiants GCP ne sont pas dans le depot et n'y seront jamais
    # (doc/RUNBOOK.md section 10). Sans eux, activer le timer produirait un echec
    # bruyant toutes les nuits a 3h30 : on pose les fichiers, on n'arme pas.
    # Un mp2 qui pointe ailleurs (section A) veut dire qu'on ne sait pas quel
    # volume porte les sauvegardes. Copier « quelque chose » dans le doute
    # remplirait le bucket d'objets qu'on ne pourra jamais remplacer.
    if [[ $MP2_STATE == divergent ]]; then
        warn "mp2 divergent (voir plus haut) — source des sauvegardes incertaine"
        ready=0
    fi
    if [[ ! -x $rclone_bin ]]; then
        warn "$rclone_bin absent — la copie hors-site ne peut pas fonctionner"
        ready=0
    fi
    if [[ ! -s $key ]]; then
        warn "$key absente — cle du compte de service"
        ready=0
    fi

    if systemctl is-enabled --quiet pgbk-offsite.timer 2>/dev/null; then
        log "  pgbk-offsite.timer deja active"
        note OK "pgbk-offsite.timer (active)"
        [[ $ready -eq 0 ]] && warn "timer actif mais prerequis manquants : la copie echouera a 3h30"
    elif [[ $ready -eq 1 ]]; then
        log "  activation de pgbk-offsite.timer"
        run systemctl enable --now pgbk-offsite.timer
        note POSE "pgbk-offsite.timer (active)"
        armed=1
    else
        warn "pgbk-offsite.timer NON active : voir les avertissements ci-dessus"
        warn "  y remedier (doc/RUNBOOK.md section 10), puis rejouer ce script"
        note KO "pgbk-offsite.timer (inactive)"
    fi

    # PREMIERE COPIE, tout de suite. Le listage du bucket ne prouve que la
    # lecture : la seule facon de savoir que les objets partent vraiment est
    # d'en envoyer. Autant que ce soit maintenant, pendant qu'un humain
    # regarde, plutot qu'a 3h30 dans un journal que personne n'ouvrira.
    if [[ $armed -eq 1 && $DO_FIRST_RUN -eq 1 && $DRY -eq 0 ]]; then
        log "  premiere copie hors-site (peut prendre plusieurs minutes)"
        if systemctl start pgbk-offsite.service; then
            log "  copie initiale terminee"
            note OK "copie hors-site initiale"
        else
            warn "la copie initiale a echoue — journalctl -u pgbk-offsite -n 60 --no-pager"
            note KO "copie hors-site initiale"
        fi
    fi
    log
}

# ------------------------------------------- G. premiere sauvegarde, secrets

# Un CT tout juste deploye n'a aucune sauvegarde avant 2h30. Tant qu'il n'y en
# a pas une, il n'y a rien a copier hors-site et rien a restaurer : la chaine
# n'est pas prouvee. On en declenche donc une, une seule fois.
first_backup() {
    log "== Premiere sauvegarde"
    local n
    n=$(ct sh -c "find ${MP2_MOUNT} -mindepth 1 -maxdepth 1 -type d -name '20*' ! -name '*.part' 2>/dev/null | wc -l" 2>/dev/null || echo 0)

    if [[ ${n:-0} -gt 0 ]]; then
        log "  $n sauvegarde(s) deja presente(s)"
        note OK "sauvegardes ($n)"
        log
        return 0
    fi
    if [[ $DO_FIRST_RUN -eq 0 ]]; then
        warn "aucune sauvegarde et --no-first-run : le CT reste sans filet"
        note KO "sauvegardes (aucune)"
        log
        return 0
    fi

    log "  aucune sauvegarde — declenchement de pg-backup.service"
    if [[ $DRY -eq 1 ]]; then
        printf '  [dry-run] ct systemctl start pg-backup.service\n'
        note POSE "premiere sauvegarde"
    elif ct systemctl start pg-backup.service; then
        ct journalctl -u pg-backup -n 12 --no-pager 2>/dev/null | sed 's/^/    /'
        note POSE "premiere sauvegarde"
    else
        warn "la premiere sauvegarde a echoue :"
        ct journalctl -u pg-backup -n 30 --no-pager 2>/dev/null | sed 's/^/    /' >&2
        note KO "premiere sauvegarde"
    fi
    log
}

# Mot de passe jetable, alphanumerique : aucun caractere a citer, ni pour SQL,
# ni pour une URL de connexion applicative.
new_password() { head -c 32 /dev/urandom | base64 | tr -d '\n=+/'; }

psql_ct() { ct sudo -u postgres psql -tAc "$1"; }

# --admin : le compte d'administration. Cree UNIQUEMENT s'il manque — rejouer
# le script ne doit jamais faire tourner un mot de passe dans le dos de
# quelqu'un qui l'a range dans OpenBao.
do_admin() {
    local role=$1 pass
    log "== Compte d'administration ($role)"
    if [[ $(psql_ct "SELECT 1 FROM pg_roles WHERE rolname='$role'" || true) == 1 ]]; then
        log "  le role $role existe deja — inchange"
        log "  mot de passe perdu ? ALTER ROLE $role PASSWORD '<nouveau>' en peer (doc/RUNBOOK.md section 5)"
        note OK "role $role"
        log
        return 0
    fi
    if [[ $DRY -eq 1 ]]; then
        printf '  [dry-run] CREATE ROLE %s LOGIN SUPERUSER\n' "$role"
        note POSE "role $role"
        log
        return 0
    fi

    pass=$(new_password)
    ct sudo -u postgres psql -v ON_ERROR_STOP=1 -q -c \
       "CREATE ROLE \"$role\" LOGIN SUPERUSER PASSWORD '$pass';"
    log "  $role / $pass"
    warn "MOT DE PASSE AFFICHE UNE SEULE FOIS — le ranger dans OpenBao maintenant"
    warn "  et ajouter la ligne hostssl correspondante dans pg_hba.conf, puis rejouer ce script"
    note POSE "role $role (mot de passe affiche)"
    log
}

# --tenant : un couple base + role, via tenant.sql lu dans le montage.
do_tenant() {
    local name=$1 pass
    log "== Locataire ($name)"
    if [[ $(psql_ct "SELECT 1 FROM pg_database WHERE datname='$name'" || true) == 1 ]]; then
        log "  la base $name existe deja — inchangee"
        note OK "locataire $name"
        log
        return 0
    fi
    if [[ $DRY -eq 1 ]]; then
        printf '  [dry-run] psql -f %s/tenant.sql -v name=%s\n' "$MP" "$name"
        note POSE "locataire $name"
        log
        return 0
    fi

    # ON_ERROR_STOP=1 : sans lui, un CREATE ROLE en echec laisserait passer le
    # CREATE DATABASE et produirait une base orpheline sans proprietaire.
    pass=$(new_password)
    ct sudo -u postgres psql -v ON_ERROR_STOP=1 -q \
       -v name="$name" -v password="$pass" -f "$MP/tenant.sql"
    log "  $name / $pass"
    warn "MOT DE PASSE AFFICHE UNE SEULE FOIS — le ranger dans OpenBao maintenant"
    warn "  puis ajouter la ligne de $name dans pg_hba.conf AVANT le reject, et rejouer ce script"
    note POSE "locataire $name (mot de passe affiche)"
    log
}

# ---------------------------------------------------------------- execution

log "CT $CTID — depot $SRC (mp1 : ct/, hote : host/)"
[[ $MODE == status ]] && log "(mode --status : aucune modification)"
log

if [[ $DO_CONTAINER -eq 1 ]]; then
    container_prereqs
    log
fi

container_setup
log
write_conf
container_pgtool
host_wrapper
host_pgtool

# La premiere sauvegarde AVANT le hors-site : sans elle, la copie initiale
# n'aurait rien a transferer et sortirait en erreur « aucune sauvegarde
# locale ». L'ordre n'est pas cosmetique.
first_backup

if [[ $DO_OFFSITE -eq 1 ]]; then
    host_packages
    rclone_config
    host_offsite
fi

# Operations a secret, uniquement sur demande explicite.
if [[ -n $ADMIN_ROLE ]]; then
    do_admin "$ADMIN_ROLE"
fi
if [[ -n $TENANT_NAME ]]; then
    do_tenant "$TENANT_NAME"
fi

checks
log

log "== Resume"
printf '%s\n' "${SUMMARY[@]}"

if [[ $DRY -eq 1 ]]; then
    log
    log "Aucune modification appliquee."
fi
