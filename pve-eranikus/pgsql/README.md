# CT PostgreSQL mutualisé — `pve-eranikus`

Cluster PostgreSQL unique servant les services LXC du nœud. Un couple
base + rôle par locataire, isolés les uns des autres.

Ce fichier ne porte que **ce qu'on tape**. Le détail — création du conteneur,
conception, pièges rencontrés en production, procédures de restauration — est
dans **[RUNBOOK.md](RUNBOOK.md)**.

| | |
|---|---|
| CTID | 200, hostname `postgresql` |
| IP | 192.168.1.56/24, passerelle 192.168.1.254 |
| Nœud | `pve-eranikus` (192.168.1.11), Debian 13 |
| PostgreSQL | 18.6, dépôt PGDG, cluster `18/main` |
| Base système | `local-lvm` (SSD 512 Go) |
| Sauvegardes | `mp2` → `/var/backups/postgresql`, 50 Go sur `data` (NVMe), 14 j |
| Hors-site | `gs://homelab-pgsql-backups-dc93212a/pve-eranikus/postgresql/`, 3h30 |
| Dépôt monté | `/root/homelab_proxmox/pve-eranikus/pgsql` → `/etc/pgsql-git` (ro) |

## Déployer, mettre à jour

**Une seule commande, depuis le nœud, sans entrer dans le CT.** Première pose
et mise à jour, c'est la même : chaque étape est conditionnelle et ne touche à
rien si l'état est déjà conforme.

```bash
cd /root/homelab_proxmox && git pull
pve-eranikus/pgsql/pg-deploy.sh
```

L'enchaîner à chaque `git pull` est le geste normal : les scripts et les unités
sont des **copies**, pas des symlinks, et ne suivent pas le `git pull` seuls.

Elle installe les paquets manquants (`rclone`, `sudo`), pose les points de
montage — dont le volume des sauvegardes —, la configuration, les scripts, les
unités systemd et `rclone.conf`, puis déclenche la première sauvegarde et la
première copie hors-site. Détail :
[runbook § 2](RUNBOOK.md#2-déploiement-depuis-lhôte--pg-deploysh).

```bash
pg-deploy.sh --status        # état de chaque élément, ne change rien
pg-deploy.sh --dry-run       # annonce ce qui serait fait
pg-deploy.sh --ctid 201      # cible un autre conteneur, et le consigne
pg-deploy.sh --restart       # force un restart au lieu d'un reload
pg-deploy.sh --no-offsite    # saute la copie hors-site
pg-deploy.sh --no-install    # n'installe aucun paquet (nœud sans réseau)
pg-deploy.sh --no-first-run  # ne déclenche ni sauvegarde ni copie initiale
```

Sur un CT déjà conforme, `--dry-run` doit annoncer **zéro modification**.

Le script se joue depuis le dépôt et n'est pas dans le `PATH` : les exemples
ci-dessous omettent le préfixe `/root/homelab_proxmox/pve-eranikus/pgsql/`.
`pgbk`, lui, est bien installé sur le nœud.

**Deux choses qu'il ne fait pas**, délibérément : créer le conteneur (script
communautaire, [§ 1](RUNBOOK.md#1-création-du-conteneur)) et déposer la clé du
compte de service GCP, qui est un secret
([§ 10](RUNBOOK.md#10-copie-hors-site-vers-gcs--pgbk-offsite)).

## Gestes courants

Tout se tape **sur le nœud**, pas dans le CT.

```bash
pgbk list                          # instantanés : âge, taille, bases
pgbk backup                        # sauvegarde immédiate
pgbk show 20260820-093240          # MANIFEST + fichiers
pgbk restore forgejo               # depuis le dernier instantané
pgbk restore forgejo 20260819      # depuis le plus récent de ce jour
pgbk verify forgejo                # contrôle ACL et propriétaires
pgbk delete 20260819-233627        # supprime un instantané (jamais le dernier)
```

Créer un compte ou un locataire — le mot de passe généré n'est affiché
**qu'une fois**, et rien ne bouge si le rôle existe déjà :

```bash
pg-deploy.sh --admin  jbwittner    # compte d'administration
pg-deploy.sh --tenant forgejo      # base + rôle d'un service
```

Reste ensuite **un** geste manuel : ajouter la ligne du locataire dans
`pg_hba.conf`, avant le `reject`, puis rejouer `pg-deploy.sh`.

Journaux :

```bash
pct exec 200 -- journalctl -u pg-backup -n 50 --no-pager   # sauvegarde locale
journalctl -u pgbk-offsite -n 50 --no-pager                # copie hors-site
journalctl -u pgbk-offsite -p warning                      # anomalies seules
```

## Où va chaque fichier

Ce répertoire porte des fichiers pour **deux machines**, côte à côte dans une
arborescence plate. Poser un fichier du mauvais côté ne produit pas d'erreur
immédiate — juste une sauvegarde qui ne part jamais.

| Fichier | Tourne sur | Installé en |
|---|---|---|
| `pg-deploy.sh` | **hôte** | joué depuis le dépôt |
| `pgbk.sh` | **hôte** et **CT** | `/usr/local/sbin/pgbk` (hôte), `/usr/local/bin/pgbk` (CT) |
| `pgbk-offsite.sh` | **hôte** | `/usr/local/bin/pgbk-offsite` |
| `pgbk-offsite.service` / `.timer` | **hôte** | `/etc/systemd/system/` de l'hôte |
| `pg-backup.sh` | **CT 200** | `/usr/local/bin/pg-backup.sh` |
| `pg-backup.service` / `.timer` | **CT 200** | `/etc/systemd/system/` du CT |
| `10-homelab.conf`, `pg_hba.conf` | **CT 200** | symlinks depuis `/etc/pgsql-git` |
| `tenant.sql` | **CT 200** | joué par `pg-deploy.sh --tenant` |

Le dataset de sauvegarde porte **deux noms selon le point de vue**, et c'est la
confusion la plus facile à faire ici :

| Vu du CT | Vu de l'hôte |
|---|---|
| `/var/backups/postgresql` | `/data/subvol-200-disk-0` |

`pg-backup.sh` écrit dans le premier, `pgbk-offsite.sh` lit le second. Ce sont
les mêmes octets.

## En cas de pépin

| Symptôme | Où regarder |
|---|---|
| Restaurer une base | [§ 8](RUNBOOK.md#8-pgbk--interface-de-gestion), ou [§ 9](RUNBOOK.md#9-restauration-manuelle) à la main |
| Récupérer une sauvegarde depuis GCS | [§ 10 — Restauration depuis GCS](RUNBOOK.md#restauration-depuis-gcs) |
| `pgbk-offsite` sort en code 3 | objet distant divergent, [§ 10](RUNBOOK.md#objet-distant-divergent--le-cas-à-traiter-à-la-main) — intervention humaine |
| `pgbk-offsite.timer` reste inactif | clé GCP, `rclone` ou `mp2` : le résumé de `pg-deploy.sh` dit lequel ([§ 10](RUNBOOK.md#installation)) |
| Erreur 400 « legacy ACL » | accès uniforme du bucket, [§ 10](RUNBOOK.md#le-piège-de-laccès-uniforme-ubla) |
| Base injoignable, service `active` | `listen_addresses` en LXC, [§ 4](RUNBOOK.md#4-pose-de-la-configuration) |
| Après restauration, isolation disparue | les ACL ne sont pas dans le dump, [§ 9](RUNBOOK.md#les-acl-ne-sont-pas-dans-le-dump) |
| CT en `243/CREDENTIALS` | nesting, [§ 1](RUNBOOK.md#le-piège-du-nesting) |

## Reste à faire

- [ ] Ligne du locataire `forgejo` dans `pg_hba.conf` — dépend de son IP
      définitive. C'est le dernier geste que `pg-deploy.sh` ne fait pas.
- [ ] Copier `postgresql.vars` dans ce dépôt après vérification des secrets.
- [x] Sauvegarde locale, `pgbk`, copie hors-site GCS, et pose complète par
      `pg-deploy.sh` — voir le runbook.
