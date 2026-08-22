#!/usr/bin/env bash
#
# init.sh — provisionnement d'une VM Forgejo NEUVE. Une seule fois.
#
# Ce n'est pas un script rejouable et il ne cherche pas à l'être : ce dépôt n'a
# plus de moteur de convergence. Il pose un système, écrit un témoin, et refuse
# de repasser. Ce qui se met à jour ensuite le fait par `sys-update.sh` et par
# `docker compose pull`.
#
# IL NE FORMATE JAMAIS RIEN. Les trois `mkfs.ext4` — étiquettes « srv »,
# « artifacts » et « backup » — sont des commandes du runbook, tapées à la
# main, une fois chacune : voir doc/RUNBOOK.md section 2. C'est le seul geste
# de tout ce montage qui puisse détruire les dépôts, et il n'a rien à faire
# dans un script qu'on lance sans relire.
#
# À jouer en root, dans une VM Debian 13 fraîchement créée (runbook § 1).
#
set -euo pipefail

TEMOIN=/var/lib/homelab/init.done
TZ_VM="${TZ_VM:-Europe/Paris}"

log() { printf '%s [INIT ] %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { printf '%s [ERROR] %s\n' "$(date '+%H:%M:%S')" "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "à lancer en root"
# Le témoin est écrit À LA FIN : un script interrompu au milieu se relance.
# En `if` et non en `[[ … ]] && die` : sous `set -e`, un test qui échoue en tête
# de liste fait sortir le script en 1 — donc l'absence du témoin, c'est-à-dire
# le cas normal, deviendrait un refus.
if [[ -e $TEMOIN ]]; then
  die "déjà provisionné le $(cat "$TEMOIN") — voir doc/RUNBOOK.md section 3"
fi

# Les volumes de données AVANT tout le reste : inutile d'installer Docker sur
# une VM dont les trois disques ne sont pas formatés. Les étiquettes sont
# vérifiées TOUTES LES TROIS, et l'absence de l'une suffit à refuser — une VM
# provisionnée avec deux volumes sur trois démarre et écrit au mauvais endroit.
#
# `-c /dev/null` : sonder les disques SANS passer par /run/blkid/blkid.tab. Le
# cache est en retard sur un mkfs tout juste fait, et sans ce drapeau le script
# refuserait de démarrer sur un volume parfaitement formaté — un refus qui
# accuse le disque alors que le fautif est le cache.
etiquette() { blkid -c /dev/null -L "$1" >/dev/null 2>&1; }

for nom in srv artifacts backup; do
  etiquette "$nom" \
    || die "aucun volume étiqueté « $nom » — le formater d'abord, voir doc/RUNBOOK.md section 2"
done

log "mise à jour du système"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get -y -qq dist-upgrade
# `git` en fait partie : l'image genericcloud ne l'a pas, et c'est lui qui
# clonera le dépôt juste après (§ 4). Ce script est donc déposé par scp la
# première fois — il est autonome, c'est tout l'intérêt d'un fichier unique.
apt-get -y -qq install ca-certificates curl git gnupg rclone qemu-guest-agent \
                       unattended-upgrades

log "montage des trois volumes de données par étiquette"
# Par LABEL et non par /dev/sdX : l'ordre d'énumération des disques ne suit pas
# les numéros de slot Proxmox (§ 9), et la lettre peut changer d'un démarrage à
# l'autre. L'étiquette, non.
#
# TROIS POINTS DE MONTAGE FRÈRES, et /srv lui-même reste sur le disque système.
# Chacun a son cycle de vie : /srv/forgejo se sauvegarde en paire, /srv/artifacts
# est repris par le vzdump, /srv/backup est explicitement hors de tout vzdump.
# Aucun n'est sous un autre : remplir l'un ne peut donc pas empêcher les autres
# d'écrire — c'est toute la raison d'être du découpage.
mkdir -p /srv/forgejo /srv/artifacts /srv/backup
for etq in srv:/srv/forgejo artifacts:/srv/artifacts backup:/srv/backup; do
  nom=${etq%%:*}
  point=${etq#*:}
  grep -q "LABEL=$nom " /etc/fstab \
    || echo "LABEL=$nom $point ext4 defaults 0 2" >> /etc/fstab
  mountpoint -q "$point" || mount "$point"
done

# APRÈS les montages, jamais avant : créés sur un point de montage vide, ces
# répertoires disparaîtraient sous le volume au premier `mount`.
mkdir -p /srv/forgejo/data /srv/forgejo/db
# 1000:1000 : Forgejo CRÉE le répertoire des artefacts au démarrage et refuse
# de démarrer s'il ne peut pas y écrire — vérifié sur l'image 15.0.7.
chown 1000:1000 /srv/artifacts

log "dépôt Docker CE officiel"
# Le dépôt de Docker, pas les paquets Debian : `docker-compose-plugin` (la
# forme `docker compose`) n'existe que là.
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian %s stable\n' \
  "$(dpkg --print-architecture)" "$(. /etc/os-release && echo "$VERSION_CODENAME")" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get -y -qq install docker-ce docker-ce-cli containerd.io docker-compose-plugin

log "rotation des journaux Docker"
# Sans ça, le json-file d'un conteneur `restart: unless-stopped` grossit sans
# limite et finit par saturer la racine — donc par arrêter la base.
cat > /etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "5" }
}
JSON
systemctl restart docker

log "l'utilisateur admin pilote la pile sans sudo"
usermod -aG docker admin

log "mises à jour automatiques : la SÉCURITÉ, et rien d'autre"
# Une source de vérité ne prend pas une version majeure de Docker à 6 h du
# matin. Les correctifs de sécurité, oui ; le reste se décide, avec un
# snapshot — voir sys-update.sh et doc/RUNBOOK.md section 6.
cat > /etc/apt/apt.conf.d/52homelab-unattended <<'CONF'
Unattended-Upgrade::Origins-Pattern {
        "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
};
Unattended-Upgrade::Automatic-Reboot "false";
CONF
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
  > /etc/apt/apt.conf.d/20auto-upgrades

log "fuseau horaire et agent invité"
timedatectl set-timezone "$TZ_VM"
systemctl enable --now qemu-guest-agent

mkdir -p "$(dirname "$TEMOIN")"
date -Is > "$TEMOIN"
log "terminé — témoin posé dans $TEMOIN"
log "suite : déployer la pile, doc/RUNBOOK.md section 4"
