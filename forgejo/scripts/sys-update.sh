#!/usr/bin/env bash
#
# sys-update.sh — mise à jour du système de la VM Forgejo.
#
# NE REDÉMARRE JAMAIS. Il signale que le redémarrage est nécessaire, dit quels
# paquets le demandent, et sort. Redémarrer une source de vérité est une
# décision qui se prend avec un `qm snapshot` pris avant, depuis le nœud — voir
# doc/RUNBOOK.md section 6.
#
# `dist-upgrade` ET NON `upgrade`. Sur Debian stable, `upgrade` retient
# silencieusement tout paquet dont la mise à jour demande d'en INSTALLER un
# nouveau. C'est exactement ce qui arrive à Docker CE quand il découpe un
# paquet ; le système se croit à jour et ne l'est pas.
#
set -euo pipefail

log() { printf '%s [SYS  ] %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { printf '%s [ERROR] %s\n' "$(date '+%H:%M:%S')" "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "à lancer en root"

export DEBIAN_FRONTEND=noninteractive

log "actualisation des listes"
apt-get update -qq

log "dist-upgrade"
apt-get -y dist-upgrade

log "retrait des paquets devenus inutiles"
apt-get -y --purge autoremove

if [[ -e /var/run/reboot-required ]]; then
  log "REDÉMARRAGE REQUIS — il n'est PAS fait ici."
  if [[ -s /var/run/reboot-required.pkgs ]]; then
    log "paquets qui le demandent :"
    sort -u /var/run/reboot-required.pkgs | sed 's/^/         /'
  fi
  log "prendre un snapshot depuis le nœud, PUIS redémarrer :"
  log "  qm snapshot 300 avant-reboot-\$(date +%Y%m%d)"
  log "  puis, dans la VM : systemctl reboot"
  log "voir doc/RUNBOOK.md section 6"
else
  log "aucun redémarrage requis"
fi

log "terminé"
