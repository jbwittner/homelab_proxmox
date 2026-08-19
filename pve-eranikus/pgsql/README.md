# CT PostgreSQL mutualisé — `pve-eranikus`

Cluster PostgreSQL unique servant les services LXC du nœud. Un couple
base + rôle par locataire, isolés les uns des autres.

Déployé le 19 août 2026 via le script communautaire `postgresql.sh`.
Le CTID appartient à la plage **200-299**, réservée par convention aux
services installés depuis un script communautaire : l'installation n'est pas
décrite dans ce dépôt, elle se rejoue en relançant le script. Seule la
configuration est versionnée ici.

| | |
|---|---|
| CTID | 200 |
| Hostname | `postgresql` |
| IP | 192.168.1.56/24 |
| Passerelle | 192.168.1.254 |
| Nœud | `pve-eranikus` (192.168.1.11) |
| OS | Debian 13 (trixie) |
| PostgreSQL | 18.6, dépôt PGDG, cluster `18/main` |
| Stockage | `local-lvm` (SSD 512 Go) — le 1 To est réservé à Forgejo |
| Dépôt monté | `/root/homelab_proxmox/pve-eranikus/pgsql` → `/etc/pgsql-git` (ro) |

## 1. Création du conteneur

```bash
var_os='debian' bash -c "$(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/postgresql.sh)"
```

Réponses au questionnaire, sauvegardées par le script dans
`/usr/local/community-scripts/defaults/postgresql.vars` — à copier ici après
avoir vérifié qu'aucun secret n'y figure (`grep -i pass`).

| Question | Réponse | Pourquoi |
|---|---|---|
| Container type | Unprivileged | PostgreSQL n'a besoin d'aucune capacité privilégiée |
| Root password | généré aléatoirement | porte de secours console ; rangé dans OpenBao |
| Container ID | 200 | premier de la plage « script communautaire » |
| Hostname | `postgresql` | défaut du script, aucune raison d'en dévier |
| FQDN | vide | aucune résolution de nom dans le chemin critique |
| Disk | 32 Go | WAL 2 Go + logs + 14 j de dumps dans `/var/backups` |
| CPU | 2 | cohérent avec `max_parallel_workers_per_gather = 2` |
| RAM | 4096 Mo | fixe `shared_buffers` 1 Go et `effective_cache_size` 3 Go |
| Bridge | vmbr0 | segment L2 unique, trafic PG interne au nœud |
| IPv4 | 192.168.1.56/24 | statique : `pg_hba` filtre en `/32`, un bail DHCP casserait tout |
| Passerelle | 192.168.1.254 | Livebox |
| IPv6 | none | — |
| MTU / DNS / MAC / VLAN | vides | héritage de l'hôte |
| Clé SSH | clé personnelle uniquement | |
| Root SSH | non | l'accès admin passe par `pct enter 200` |
| FUSE / TUN / mknod / mount FS | non | inutiles à un démon PostgreSQL |
| **Nesting** | **oui** | **obligatoire sur Debian 13** — voir ci-dessous |
| Protection | oui | ce CT porte les données de tous les services |
| Timezone | Europe/Paris | concorde avec `timezone` du drop-in |
| APT Cacher / proxy | non | |
| Post-install hook | vide | à écrire plus tard (voir « Reste à faire ») |
| Version PostgreSQL | 18 | supportée jusqu'en novembre 2030 |
| Adminer | non | interface web PHP non suivie, sur le CT le plus sensible |

### Le piège du nesting

Le script demande le nesting **avant** d'afficher l'avertissement qui explique
pourquoi il le faut, et la réponse n'est pas rattrapable ensuite : il faut tout
recommencer. Répondre **oui** directement.

Depuis systemd 254, les unités utilisent le mécanisme de *credentials*, qui
exige de monter un tmpfs — impossible pour un CT non privilégié avec le profil
AppArmor standard, d'où l'erreur `243/CREDENTIALS` et un conteneur qui démarre
en état dégradé. `nesting=1` bascule sur le profil
`lxc-container-default-nesting` qui l'autorise. Cela concerne aussi les
directives `PrivateTmp` et `NoNewPrivileges` de `pg-backup.service`.

### Ce que pose le script

`initdb` est lancé en `--auth-local peer --auth-host scram-sha-256`, locale
`C.UTF-8`. Aucun mot de passe n'est généré et aucun fichier de credentials
n'est écrit : le cluster est livré nu, l'accès se fait en peer depuis
l'intérieur. Le paquet `postgresql-18-jit` est installé (désactivé par le
drop-in, voir plus bas), et `ssl = on` avec le certificat snakeoil est déjà
actif — aucun `ssl-cert` à installer.

### Après création

```bash
pct set 200 --startup order=1          # PostgreSQL avant ses locataires
pct config 200 | grep -E 'net0|features|protection'
pct exec 200 -- systemctl status fstrim.timer   # indispensable sur LVM-thin
```

Le stockage est du **LVM-thin**, pas du ZFS : aucun réglage de `recordsize` à
faire. Deux conséquences en revanche — le pool est surprovisionné (surveiller
`lvs`, un pool saturé arrête net le serveur), et `full_page_writes` doit rester
à `on`, ext4 sur LVM n'offrant aucune garantie d'atomicité des écritures de
page.

## 2. Montage du dépôt

La protection du CT interdit toute modification de disque, ajout d'un point de
montage compris. Il faut la lever puis la remettre :

```bash
pct set 200 --protection 0
pct set 200 --mp1 /root/homelab_proxmox/pve-eranikus/pgsql,mp=/etc/pgsql-git,ro=1
pct reboot 200                         # un mp n'est pris en compte qu'au démarrage
pct set 200 --protection 1
pct config 200 | grep -E 'protection|mp1'
```

Dans le CT, les fichiers apparaissent en `nobody:nogroup` : c'est le décalage
d'UID de 100000 propre aux conteneurs non privilégiés. Sans conséquence, les
fichiers étant en 644 et le montage en lecture seule.

## 3. Pose de la configuration

Les deux fichiers sont des **liens symboliques** vers le dépôt. PostgreSQL
accepte un symlink pour `pg_hba.conf` malgré ses exigences de permissions,
vérifié sur cette instance.

```bash
pct enter 200
ln -sf /etc/pgsql-git/10-homelab.conf /etc/postgresql/18/main/conf.d/10-homelab.conf
ln -sf /etc/pgsql-git/pg_hba.conf     /etc/postgresql/18/main/pg_hba.conf
systemctl restart postgresql           # listen_addresses exige un restart
```

`postgresql.conf` **n'est jamais modifié** : il se termine par
`include_dir = 'conf.d'`, donc le drop-in est lu après lui et l'emporte. Le
fichier du paquet peut ainsi évoluer avec `apt` sans conflit ni `.dpkg-dist` à
arbitrer.

Attention à `postgresql.auto.conf`, dans `/var/lib/postgresql/18/main/` : écrit
par les `ALTER SYSTEM SET`, il est lu **en dernier** et écrase le drop-in.
`ALTER SYSTEM RESET <param>;` pour le nettoyer.

### Mise à jour de la configuration

```bash
cd /root/homelab_proxmox && git pull
pct exec 200 -- systemctl reload postgresql
```

Un `reload` suffit pour `pg_hba.conf` et la plupart des paramètres. Seuls
`listen_addresses`, `shared_buffers`, `max_connections` et les autres
paramètres marqués *postmaster* exigent un `restart`.

### Vérifications

```bash
pct exec 200 -- sudo -u postgres psql -c \
  "SELECT name, setting, sourcefile FROM pg_settings WHERE sourcefile IS NOT NULL;"
pct exec 200 -- sudo -u postgres psql -c \
  "SELECT line_number, type, database, user_name, address, auth_method FROM pg_hba_file_rules;"
pct exec 200 -- ss -lntp | grep 5432
```

`pg_hba_file_rules` est la vérification qui compte : un `reload` réussi ne
prouve pas que le fichier a été relu, PostgreSQL conservant l'ancienne
configuration en mémoire si le nouveau est invalide. Une colonne `error` non
vide signale une règle mal formée.

État attendu après pose :

```
 line_number |  type   | database |  user_name  |   address    |  auth_method
-------------+---------+----------+-------------+--------------+---------------
          15 | local   | {all}    | {postgres}  |              | peer
          16 | local   | {all}    | {all}       |              | peer
          19 | host    | {all}    | {all}       | 127.0.0.1    | scram-sha-256
          24 | hostssl | {all}    | {jbwittner} | 192.168.1.11 | scram-sha-256
          34 | host    | {all}    | {all}       | 0.0.0.0      | reject
```

Et `ss` ne doit plus montrer que `192.168.1.56:5432` et `127.0.0.1:5432` —
le `listen_addresses = '*'` du paquet est resserré par le drop-in.

## 4. Compte d'administration (`jbwittner`)

Créé depuis l'intérieur du CT, en peer sur socket Unix :

```bash
pct enter 200
PASS="$(head -c 32 /dev/urandom | base64 | tr -d '\n=+/')"
sudo -u postgres psql -c "CREATE ROLE jbwittner LOGIN SUPERUSER PASSWORD '${PASS}';"
echo "$PASS"      # → OpenBao immédiatement, il ne réapparaîtra pas
```

Mot de passe perdu ? Aucun blocage possible tant que la ligne `local all
postgres peer` existe — c'est la porte de secours, ne jamais la supprimer :

```bash
sudo -u postgres psql -c "ALTER ROLE jbwittner PASSWORD '<nouveau>';"
```

Le nom du rôle dans `pg_hba.conf` doit correspondre **exactement**, sinon
aucune règle ne matche et la connexion est refusée sans message explicite.

### Connexion depuis le Mac

Par tunnel SSH : un poste mobile n'a rien à faire dans un fichier qui décrit
l'infrastructure, et le tunnel fonctionne encore depuis l'extérieur.

```bash
ssh -L 5432:192.168.1.56:5432 root@192.168.1.11
```

DBeaver : hôte `localhost`, port `5432`, base `postgres`, utilisateur
`jbwittner`, SSL `require` (le certificat étant auto-signé, surtout pas
`verify-full`). DBeaver sait monter le tunnel lui-même — onglet *SSH*, hôte
`192.168.1.11`.

**Le champ *Host* de la connexion principale doit alors contenir
`192.168.1.56`**, c'est-à-dire l'adresse vue depuis le nœud une fois le tunnel
établi. Y mettre `localhost` fait chercher PostgreSQL sur le nœud Proxmox
lui-même et produit un « Connection reset ».

PostgreSQL voit la connexion arriver de `192.168.1.11`, l'IP du nœud, et non
celle du Mac — d'où la règle `/32` sur cette adresse.

Le `SUPERUSER` contourne le `REVOKE CONNECT` posé sur chaque base de
locataire : pratique pour administrer, mais à ne pas utiliser comme
identifiant de consultation courante — une requête maladroite sur la base d'un
service passera sans garde-fou.

## 5. Ajout d'un locataire

```bash
NAME=forgejo
PASS="$(head -c 32 /dev/urandom | base64 | tr -d '\n=+/')"
sudo -u postgres psql -v ON_ERROR_STOP=1 \
     -v name="$NAME" -v password="$PASS" \
     -f /etc/pgsql-git/tenant.sql
echo "$NAME / $PASS"      # → OpenBao
```

Le nom est une variable, pas un motif à substituer : impossible de créer un
rôle nommé d'après le gabarit par étourderie. Et `psql` cite lui-même les
identifiants et les chaînes, donc aucun caractère n'est interdit dans le mot
de passe — contrairement à une substitution `sed`, que `|` ou `&` cassent.

Le `ON_ERROR_STOP=1` n'est pas décoratif : sans lui, un `CREATE ROLE` qui
échoue laisserait passer le `CREATE DATABASE` et produirait une base orpheline
sans propriétaire.

Erreur de nom ? La base d'abord, le rôle ensuite :

```bash
sudo -u postgres psql -c "DROP DATABASE <nom>;"
sudo -u postgres psql -c "DROP ROLE <nom>;"
```

Ajouter la ligne correspondante dans `pg_hba.conf`, **avant** le `reject`, puis
`git pull` sur l'hôte et `systemctl reload postgresql`. Côté client :
`SSL_MODE = require`.

Dans les configurations applicatives, préférer un nom de domaine à l'IP — mais
le déclarer dans le `/etc/hosts` du CT client plutôt que de dépendre d'AdGuard,
sans quoi le service ne peut plus joindre sa base tant que le DNS n'est pas
debout.

## 6. Sauvegarde

```bash
pct enter 200
install -m 644 /etc/pgsql-git/pg-backup.service /etc/systemd/system/
install -m 644 /etc/pgsql-git/pg-backup.timer   /etc/systemd/system/
install -m 755 /etc/pgsql-git/pg-backup.sh      /usr/local/bin/pg-backup.sh
systemctl daemon-reload && systemctl enable --now pg-backup.timer
systemctl start pg-backup.service && journalctl -u pg-backup -n 20
```

L'unité pointe vers `/usr/local/bin/pg-backup.sh` : le script doit être copié,
pas lié, car le montage est en lecture seule et ne peut pas porter le bit
d'exécution.

Un `.dump` par base plus un `globals-*.sql` par exécution, 14 jours de
rétention dans `/var/backups/postgresql`.

**Le fichier globals est le plus facile à oublier et le plus coûteux à
perdre** : les rôles et leurs mots de passe ne figurent dans aucun `pg_dump` de
base. Sans lui, une restauration rend les données sans les comptes qui y
accèdent.

**Ces dumps vivent sur le même disque que la base qu'ils protègent.** Deux
copies sur le même SSD ne survivent pas à une panne matérielle : c'est le
`vzdump` et la copie hors-site qui couvrent ce risque, pas ce timer.

## 7. Restauration

```bash
# Une seule base, les autres locataires restent en ligne.
sudo -u postgres dropdb forgejo
sudo -u postgres createdb forgejo -O forgejo -T template0 \
     --encoding UTF8 --lc-collate C --lc-ctype C
sudo -u postgres pg_restore -d forgejo --no-owner --role=forgejo \
     /var/backups/postgresql/forgejo-<STAMP>.dump
```

Reconstruction complète : recréer le cluster, rejouer `globals-*.sql`, puis les
dumps un par un.

**La protection du conteneur bloque la restauration d'un `vzdump` par-dessus le
CTID 200**, l'opération détruisant le CT avant de le recréer. Prévoir
`pct set 200 --protection 0` au préalable — c'est le second endroit où la
protection se met en travers, après l'ajout d'un point de montage.

## Reste à faire

- [ ] Installer le timer de sauvegarde (section 6).
- [ ] Ligne du locataire `forgejo` — dépend de son IP définitive.
- [ ] Copie hors-site des dumps vers GCS.
- [ ] Copier `postgresql.vars` dans ce dépôt après vérification des secrets.
- [ ] Hook post-install (`pct set`, montage, symlinks, timer) pour rendre
      l'ensemble rejouable. À écrire à partir de ce qui a réellement
      fonctionné, pas avant.

## Notes

- Ce CT est un point de défaillance unique pour tous les services du nœud.
  C'est le prix assumé de la mutualisation ; une montée de version majeure
  devient une fenêtre de maintenance pour tout le monde.
- Montée de majeure : `pg_upgradecluster` (outillage `postgresql-common`,
  spécifique Debian), après snapshot du CT. PostgreSQL 18 conserve les
  statistiques du planner à la migration, il n'y a plus d'`ANALYZE` massif à
  lancer ensuite.
- `jit = off` : le paquet `postgresql-18-jit` est installé, mais sur une charge
  OLTP la compilation à la volée coûte plus qu'elle ne rapporte et produit des
  latences erratiques.
- `log_connections` n'est plus un booléen depuis PostgreSQL 18 mais une liste
  de types d'événements. Un `= on` empêcherait le démarrage.
- `work_mem` est **par nœud de tri et par connexion**. À 100 connexions et
  8 Mo, le pire cas théorique dépasse la RAM du CT : surveiller
  `log_temp_files` plutôt que d'augmenter à l'aveugle.