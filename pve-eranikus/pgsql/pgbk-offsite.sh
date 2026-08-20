#!/usr/bin/env bash
#
# pgbk-offsite.sh — copie hors-site des sauvegardes PostgreSQL vers GCS.
#
# ┌──────────────────────────────────────────────────────────────────────────┐
# │ CE SCRIPT TOURNE SUR L'HÔTE PROXMOX, PAS DANS LE CT.                     │
# │ Installé en /usr/local/bin/pgbk-offsite sur pve-eranikus.                │
# │ Il lit le dataset de sauvegarde par sa vue HÔTE (/data/subvol-200-disk-0)│
# │ et non par sa vue CT (/var/backups/postgresql).                          │
# └──────────────────────────────────────────────────────────────────────────┘
#
# POURQUOI SUR L'HÔTE — le CT PostgreSQL est le composant le plus sensible du
# nœud. Il n'a aucune raison de détenir des identifiants GCP ni d'atteindre
# internet. L'hôte lit directement le dataset ZFS, ce qui mutualise aussi
# rclone pour les futurs services du nœud.
#
# ARBORESCENCE DISTANTE — le nœud au premier niveau, pour que vert-ysera
# puisse s'ajouter sans restructurer :
#
#   gs://<bucket>/pve-eranikus/postgresql/20260820-093240/{*.dump,globals.sql,MANIFEST}
#
# COPY, JAMAIS SYNC — « rclone sync » réplique les suppressions : un bug local,
# un dataset démonté, un rm malheureux, et la copie distante disparaît avec
# l'originale. L'interdiction est structurante, pas cosmétique. La rétention
# distante est faite côté serveur par la règle de cycle de vie du bucket
# (Nearline 30 j, Coldline 90 j, suppression 365 j), jamais par ce script.
#
# DROITS DÉLIBÉRÉMENT INCOMPLETS — le compte de service a objectViewer +
# objectCreator sur le seul bucket. Il peut lister, lire et créer ; il ne peut
# ni écraser ni supprimer. Un nœud compromis ne peut donc pas détruire
# l'historique distant. Conséquence directe sur ce code :
#
#   - le transfert se fait en --ignore-existing : on ne tente JAMAIS un
#     écrasement, qui serait de toute façon refusé en 403 ;
#   - un transfert interrompu peut laisser un objet partiel que rien ici ne
#     pourra remplacer. C'est le mode de panne le plus probable de tout ce
#     montage. Le contrôle post-transfert le détecte et le SIGNALE comme une
#     anomalie demandant une intervention humaine (compte personnel). Le
#     script ne tente pas de le réparer, et surtout pas en boucle.
#
# JOURNALISATION — identique à pg-backup.sh : sortie horodatée et préfixée par
# niveau, lisible à l'écran comme dans journalctl, trap sur ERR consignant la
# ligne fautive. Diagnostiquer une copie qui a mal tourné trois semaines plus
# tôt ne doit pas demander de rejouer le script.
#
# Usage :
#   pgbk-offsite              copie les instantanés absents du bucket
#   pgbk-offsite --dry-run    dit ce qui serait transféré, n'envoie rien
#   pgbk-offsite --help       cette aide
#
# Codes de retour :
#   0  tout est en ligne
#   1  environnement inutilisable (rclone, clé, bucket, aucune sauvegarde)
#   2  au moins un transfert a échoué
#   3  au moins un objet distant diverge de sa source — intervention humaine
#
set -Eeuo pipefail

# ─── Paramétrage ─────────────────────────────────────────────────────────────
# Valeurs par défaut ici, valeurs réelles dans pgbk-offsite.service : l'unité
# systemd est l'endroit unique où l'on décrit ce nœud-ci.

: "${PGBK_OFFSITE_NODE:=$(hostname -s)}"       # premier niveau distant
: "${PGBK_OFFSITE_SRC:=/data/subvol-200-disk-0}"   # VUE HÔTE du dataset
: "${PGBK_OFFSITE_REMOTE:=gcs}"                # remote déclaré dans rclone.conf
: "${PGBK_OFFSITE_BUCKET:=homelab-pgsql-backups-dc93212a}"
: "${PGBK_OFFSITE_SUBPATH:=postgresql}"        # sous le nœud, le service
: "${PGBK_OFFSITE_CONFIG:=/root/.config/rclone/rclone.conf}"
: "${PGBK_OFFSITE_KEY:=/root/.config/rclone/pgsql-backups.json}"
: "${PGBK_OFFSITE_RCLONE:=/usr/bin/rclone}"    # chemin absolu : PATH systemd minimal
: "${PGBK_OFFSITE_TRANSFERS:=4}"
: "${PGBK_OFFSITE_RETRIES:=3}"
: "${PGBK_OFFSITE_BWLIMIT:=}"                  # vide = pas de bridage
: "${PGBK_OFFSITE_CHECK:=hash}"                # hash | size
: "${PGBK_OFFSITE_STALE_HOURS:=48}"            # âge au-delà duquel on alerte

PREFIX="${PGBK_OFFSITE_NODE}/${PGBK_OFFSITE_SUBPATH}"
BASE="${PGBK_OFFSITE_REMOTE}:${PGBK_OFFSITE_BUCKET}/${PREFIX}"
DRY=0

# ─── Journalisation ──────────────────────────────────────────────────────────
# Sous systemd, stdout et stderr partent dans le journal. L'horodatage fait
# doublon avec celui de journalctl mais rend le script utilisable à la main.

log()   { printf '%s [INFO ] %s\n'  "$(date '+%H:%M:%S')" "$*"; }
warn()  { printf '%s [WARN ] %s\n'  "$(date '+%H:%M:%S')" "$*" >&2; }
error() { printf '%s [ERROR] %s\n'  "$(date '+%H:%M:%S')" "$*" >&2; }
step()  { printf '%s [STEP ] %s\n'  "$(date '+%H:%M:%S')" "$*"; }
die()   { error "$*"; exit 1; }

# Sortie de rclone, décalée pour se distinguer de nos propres lignes.
indent() { sed 's/^/         /'; }

on_error() {
  local rc=$? line=$1
  error "échec ligne ${line} (code ${rc}) — après ${SECONDS}s"
  error "copie hors-site NON garantie pour cette exécution"
  exit "$rc"
}
trap 'on_error $LINENO' ERR
trap 'error "interrompu par signal"; exit 130' INT TERM

usage() {
  awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)     DRY=1; shift ;;
    -h|--help)     usage ;;
    *)             die "argument inconnu : $1 (voir pgbk-offsite --help)" ;;
  esac
done

# ─── Contrôles préalables ────────────────────────────────────────────────────
# Tout ce qui peut manquer est vérifié AVANT le premier transfert, avec un
# message qui dit quoi faire. Un échec silencieux est pire qu'une absence de
# sauvegarde : on croirait avoir une copie hors-site.

step "démarrage — ${PGBK_OFFSITE_SRC} → ${BASE}"

[[ $EUID -eq 0 ]] \
  || die "à lancer en root sur le nœud : les dumps sont en 600, propriété d'un UID de CT non privilégié"

[[ -x $PGBK_OFFSITE_RCLONE ]] \
  || die "rclone introuvable : ${PGBK_OFFSITE_RCLONE} — l'installer : apt install rclone"

[[ -r $PGBK_OFFSITE_CONFIG ]] \
  || die "configuration rclone absente ou illisible : ${PGBK_OFFSITE_CONFIG}"

[[ -s $PGBK_OFFSITE_KEY ]] \
  || die "clé du compte de service absente ou vide : ${PGBK_OFFSITE_KEY}
         elle est hors dépôt par construction — la reposer depuis le gestionnaire de secrets"

# La clé est un JSON de compte de service : une clé tronquée ou un fichier
# collé de travers se voit ici, avant de partir sur un 401 incompréhensible.
grep -q '"private_key"' "$PGBK_OFFSITE_KEY" \
  || die "clé invalide : ${PGBK_OFFSITE_KEY} ne contient pas de champ private_key"

[[ -d $PGBK_OFFSITE_SRC ]] \
  || die "source absente : ${PGBK_OFFSITE_SRC}
         c'est la VUE HÔTE du dataset ; dans le CT il s'appelle /var/backups/postgresql
         vérifier que le CT est démarré et le dataset monté"

log "  rclone $("$PGBK_OFFSITE_RCLONE" version | awk 'NR==1 {print $2}') | ${PGBK_OFFSITE_TRANSFERS} transfert(s) parallèle(s) | contrôle par ${PGBK_OFFSITE_CHECK}"
if [[ $DRY -eq 1 ]]; then warn "  --dry-run : aucun objet ne sera écrit"; fi

# Options communes. --config explicite : sous systemd, HOME peut ne pas être
# celui qu'on croit, et rclone chercherait son fichier ailleurs.
RCLONE=(
  "$PGBK_OFFSITE_RCLONE"
  --config "$PGBK_OFFSITE_CONFIG"
  --retries "$PGBK_OFFSITE_RETRIES"
  --low-level-retries 3
  --stats 0
)
if [[ -n $PGBK_OFFSITE_BWLIMIT ]]; then RCLONE+=(--bwlimit "$PGBK_OFFSITE_BWLIMIT"); fi

CHECK_OPTS=(--one-way)   # ce qui est distant en trop ne nous regarde pas
if [[ $PGBK_OFFSITE_CHECK == size ]]; then CHECK_OPTS+=(--size-only); fi

# ─── Joignabilité du bucket ──────────────────────────────────────────────────
# Un listage suffit à prouver que la clé est valide, que le réseau passe et que
# le bucket existe. On ne demande pas les métadonnées du bucket lui-même :
# objectViewer ne donne pas storage.buckets.get, et un « rclone about »
# échouerait pour une raison sans rapport avec la santé du montage.

step "joignabilité du bucket"
if ! probe="$("${RCLONE[@]}" lsf --max-depth 1 "${PGBK_OFFSITE_REMOTE}:${PGBK_OFFSITE_BUCKET}" 2>&1)"; then
  error "bucket injoignable : ${PGBK_OFFSITE_REMOTE}:${PGBK_OFFSITE_BUCKET}"
  printf '%s\n' "$probe" | indent >&2
  error "causes usuelles : clé révoquée, droits IAM retirés, pas de sortie internet"
  exit 1
fi
log "  OK — $(printf '%s' "$probe" | grep -c . || true) entrée(s) à la racine du bucket"

# ─── Inventaire local ────────────────────────────────────────────────────────
# Trois choses ne partent jamais :
#   latest         symlink ABSOLU vers un chemin qui n'existe que dans le CT,
#                  donc cassé vu de l'hôte ; « -type d » l'écarte déjà, le
#                  filtre par nom est là pour que la règle soit lisible.
#   pre-restore-*  filets posés par « pgbk restore » avant d'écraser une base.
#                  Locaux, temporaires, sans valeur distante.
#   *.part         exécution en cours ou interrompue. Par construction de
#                  pg-backup.sh, un répertoire SANS ce suffixe est complet.

step "inventaire local"
mapfile -t SNAPSHOTS < <(
  find "$PGBK_OFFSITE_SRC" -mindepth 1 -maxdepth 1 -type d \
       -name '20*' \
       ! -name '*.part' \
       ! -name 'pre-restore-*' \
       ! -name 'latest' \
    | sort
)

if [[ ${#SNAPSHOTS[@]} -eq 0 ]]; then
  error "aucune sauvegarde locale dans ${PGBK_OFFSITE_SRC}"
  error "rien à copier hors-site — vérifier le timer du CT : pct exec 200 -- systemctl status pg-backup.timer"
  exit 1
fi
log "  ${#SNAPSHOTS[@]} instantané(s) éligible(s), du $(basename "${SNAPSHOTS[0]}") au $(basename "${SNAPSHOTS[-1]}")"

# Une source qui ne bouge plus produirait des exécutions parfaitement vertes
# tout en ne protégeant plus rien. Le dire, sans faire échouer : le hors-site
# n'est pas responsable de la sauvegarde locale.
newest_age_h=$(( ( $(date +%s) - $(stat -c %Y "${SNAPSHOTS[-1]}") ) / 3600 ))
if [[ $newest_age_h -gt $PGBK_OFFSITE_STALE_HOURS ]]; then
  warn "  le dernier instantané local a ${newest_age_h} h (seuil ${PGBK_OFFSITE_STALE_HOURS} h)"
  warn "  la sauvegarde du CT ne tourne peut-être plus"
fi

# ─── Transfert d'un instantané ───────────────────────────────────────────────
# Renvoie : 0 déjà en ligne, 10 transféré, 2 transfert en échec,
#           3 objet distant divergent (anomalie non réparable ici).

TRANSFERRED=0 ALREADY=0 FAILED=0 ANOMALY=0

push_snapshot() {
  local dir="$1" name dest out
  local -a local_files remote_files missing
  name="$(basename "$dir")"
  dest="${BASE}/${name}"

  mapfile -t local_files < <(find "$dir" -type f -printf '%P\n' | sort)
  if [[ ${#local_files[@]} -eq 0 ]]; then
    warn "  ${name} : répertoire vide — ignoré"
    return 0
  fi

  # Un instantané complet porte toujours son MANIFEST. Son absence n'empêche
  # pas la copie — des dumps sans manifeste valent mieux que rien — mais elle
  # mérite d'être dite.
  [[ -f "${dir}/MANIFEST" ]] || warn "  ${name} : pas de MANIFEST"

  # Listage distant. Sur un stockage objet, un préfixe inexistant renvoie une
  # liste vide et un code 0 : pas besoin de tester l'existence séparément.
  if ! out="$("${RCLONE[@]}" lsf --files-only -R "$dest" 2>&1)"; then
    error "  ${name} : listage distant impossible"
    printf '%s\n' "$out" | indent >&2
    return 2
  fi
  mapfile -t remote_files < <(printf '%s' "$out" | sort)

  mapfile -t missing < <(
    comm -23 <(printf '%s\n' "${local_files[@]}") \
             <(printf '%s\n' "${remote_files[@]}")
  )

  if [[ ${#missing[@]} -eq 0 ]]; then
    log "  ${name} : ${#local_files[@]} objet(s) déjà en ligne"
  else
    log "  ${name} : ${#missing[@]}/${#local_files[@]} objet(s) à envoyer ($(du -sh --apparent-size "$dir" | cut -f1) au total)"
    if [[ $DRY -eq 1 ]]; then
      printf '%s\n' "${missing[@]}" | indent
      return 10
    fi

    # copy, jamais sync — voir l'en-tête.
    # --ignore-existing : on ne tente jamais d'écraser. Sans droit
    # objects.delete l'écrasement partirait en 403 à chaque exécution, et une
    # reprise en boucle sur un objet partiel masquerait le vrai problème au
    # lieu de le montrer. Ce qui est déjà là est vérifié plus bas, pas remplacé.
    if ! "${RCLONE[@]}" copy "$dir" "$dest" \
           --ignore-existing \
           --transfers "$PGBK_OFFSITE_TRANSFERS" 2>&1 | indent; then
      error "  ${name} : transfert en échec"
      return 2
    fi
  fi

  # ─── Contrôle post-transfert ───────────────────────────────────────────────
  # Le contrôle porte sur TOUT l'instantané, pas seulement sur ce qui vient
  # d'être envoyé : c'est ici, et nulle part ailleurs, qu'un objet partiel
  # laissé par une exécution interrompue se révèle.
  if [[ $DRY -eq 1 ]]; then return 0; fi

  if ! out="$("${RCLONE[@]}" check "$dir" "$dest" "${CHECK_OPTS[@]}" 2>&1)"; then
    error "  ${name} : le distant DIVERGE de la source"
    printf '%s\n' "$out" | indent >&2
    error "  ces objets ne peuvent pas être corrigés depuis ce nœud : le compte de"
    error "  service n'a pas le droit d'écraser (objectCreator sans objects.delete)."
    error "  INTERVENTION HUMAINE, depuis un poste avec le compte personnel :"
    error "    gcloud storage rm gs://${PGBK_OFFSITE_BUCKET}/${PREFIX}/${name}/<objet>"
    error "  puis rejouer : systemctl start pgbk-offsite.service"
    return 3
  fi

  if [[ ${#missing[@]} -eq 0 ]]; then return 0; fi
  log "  ${name} : contrôle OK, ${#missing[@]} objet(s) transféré(s)"
  return 10
}

# ─── Boucle principale ───────────────────────────────────────────────────────
# Un instantané en échec n'arrête pas les autres : mieux vaut sauver les neuf
# qui passent et signaler le dixième. Le bilan final porte le verdict.

step "copie vers ${BASE}"
for snap in "${SNAPSHOTS[@]}"; do
  rc=0
  push_snapshot "$snap" || rc=$?
  case $rc in
    0)  ALREADY=$((ALREADY+1)) ;;
    10) TRANSFERRED=$((TRANSFERRED+1)) ;;
    2)  FAILED=$((FAILED+1)) ;;
    3)  ANOMALY=$((ANOMALY+1)) ;;
    *)  FAILED=$((FAILED+1)); error "  $(basename "$snap") : code inattendu ${rc}" ;;
  esac
done

# ─── Bilan ───────────────────────────────────────────────────────────────────
# À partir d'ici on ne meurt plus sur ERR : le code de retour est calculé, et
# les lignes de bilan doivent sortir même si « rclone size » n'aboutit pas.
trap - ERR

# En --dry-run, TRANSFERRED compte ce qui PARTIRAIT : le dire, sinon le bilan
# se lit comme une exécution réelle.
VERB="transféré(s)"
if [[ $DRY -eq 1 ]]; then VERB="à transférer"; fi
step "bilan — ${TRANSFERRED} ${VERB}, ${ALREADY} déjà en ligne, ${FAILED} en échec, ${ANOMALY} divergent(s)"
if [[ $DRY -eq 0 ]]; then
  remote_size="$("${RCLONE[@]}" size "$BASE" 2>/dev/null | tr '\n' ' ' || true)"
  if [[ -n ${remote_size:-} ]]; then log "  distant : ${remote_size}"; fi
fi

if [[ $ANOMALY -gt 0 ]]; then
  error "terminé en ${SECONDS}s — ${ANOMALY} instantané(s) divergent(s), voir ci-dessus"
  error "la copie hors-site est INCOMPLÈTE tant que ce n'est pas traité à la main"
  exit 3
fi
if [[ $FAILED -gt 0 ]]; then
  error "terminé en ${SECONDS}s — ${FAILED} instantané(s) en échec"
  exit 2
fi

step "terminé en ${SECONDS}s — ${#SNAPSHOTS[@]} instantané(s) en ligne sur ${BASE}"
