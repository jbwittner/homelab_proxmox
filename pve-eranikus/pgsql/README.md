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
| Déploiement | `pg-deploy.sh`, joué sur le nœud |
| Exploitation | `pgbk.sh`, joué sur le nœud |
| Hors-site | `pgbk-offsite.sh`, joué sur le nœud (section 10) |

### Où va chaque fichier

Ce répertoire porte désormais des fichiers pour **deux machines**. Ils sont
côte à côte dans une arborescence plate ; l'endroit où chacun s'installe n'est
donc lisible qu'ici. Poser un fichier du mauvais côté ne produit pas d'erreur
immédiate — juste une sauvegarde qui ne part jamais, ou un timer qui ne trouve
rien.

| Fichier | Tourne sur | Installé en |
|---|---|---|
| `pg-deploy.sh` | **hôte** `pve-eranikus` | joué depuis le dépôt |
| `pgbk.sh` | **hôte** et **CT** | `/usr/local/sbin/pgbk` (hôte), `/usr/local/bin/pgbk` (CT) |
| `pgbk-offsite.sh` | **hôte** `pve-eranikus` | `/usr/local/bin/pgbk-offsite` |
| `pgbk-offsite.service` / `.timer` | **hôte** `pve-eranikus` | `/etc/systemd/system/` de l'hôte |
| `pg-backup.sh` | **CT 200** | `/usr/local/bin/pg-backup.sh` |
| `pg-backup.service` / `.timer` | **CT 200** | `/etc/systemd/system/` du CT |
| `10-homelab.conf`, `pg_hba.conf` | **CT 200** | symlinks depuis `/etc/pgsql-git` |
| `tenant.sql` | **CT 200** | joué à la main via `psql -f` |

Le dataset de sauvegarde porte **deux noms selon le point de vue**, et c'est la
confusion la plus facile à faire dans ce répertoire :

| Vu du CT | Vu de l'hôte |
|---|---|
| `/var/backups/postgresql` | `/data/subvol-200-disk-0` |

`pg-backup.sh` écrit dans le premier, `pgbk-offsite.sh` lit le second. Ce sont
les mêmes octets.

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

## 2. Déploiement depuis l'hôte — `pg-deploy.sh`

Tout ce que décrivent les sections 3, 4 et 7 se joue en une commande, **depuis
le nœud, sans entrer dans le CT** :

```bash
cd /root/homelab_proxmox && git pull
pve-eranikus/pgsql/pg-deploy.sh
```

**Première pose et mises à jour, c'est le même script**, et il n'y a pas de
raison de distinguer les deux : chaque étape est conditionnelle et ne touche à
rien si l'état est déjà conforme.

L'enchaîner à chaque `git pull` n'est pas une précaution, c'est le geste
normal. Les fichiers de configuration sont des symlinks vers le dépôt et
suivent donc le `git pull` tout seuls — mais `pgbk.sh`, `pg-backup.sh` et les
unités systemd sont des **copies**, imposées par un montage en lecture seule
qui ne peut pas porter le bit d'exécution. Modifier `pgbk.sh` dans le dépôt ne
change donc rien tant que `pg-deploy.sh` n'a pas été rejoué.

```bash
pve-eranikus/pgsql/pg-deploy.sh --status   # état de chaque élément, ne change rien
pve-eranikus/pgsql/pg-deploy.sh --dry-run  # annonce ce qui serait fait
pve-eranikus/pgsql/pg-deploy.sh --ctid 201 # cible un autre conteneur, et le consigne
pve-eranikus/pgsql/pg-deploy.sh --restart  # force un restart au lieu d'un reload
```

Sur un CT déjà conforme, `--dry-run` doit annoncer **zéro modification** : c'est
le contrôle qui prouve que le script décrit bien l'état existant, et non un
état voisin.

Ce qu'il déploie, dans l'ordre : point de montage et protection (section 3),
symlinks de configuration (section 4), unités systemd et scripts de sauvegarde
(sections 7 et 8), fichier `/etc/default/pgbk`, `pgbk` sur l'hôte,
`pgbk-offsite` et ses unités sur l'hôte (section 10),
puis les contrôles de la section 4. Il se termine par un résumé d'une ligne par
élément (`OK` / `POSE` / `KO`).

Il ne crée pas le conteneur : cela reste l'affaire du script communautaire
(section 1).

### Le CTID n'est écrit qu'à un seul endroit

`pg-deploy.sh` consigne le conteneur qu'il vient de poser dans
`/etc/default/pgbk` :

```
PG_CTID=200
```

`pgbk` le relit de là. Changer de conteneur ne demande donc que de rejouer
`pg-deploy.sh --ctid <ID>` — il n'y a pas de second fichier à penser à mettre à
jour, et `pgbk` **refuse de démarrer** si rien n'est consigné plutôt que de
taper dans un CT supposé. Priorité : `--ctid`, puis `$PG_CTID`, puis le
fichier.

## 3. Montage du dépôt

Posé par `pg-deploy.sh` (section 2). Ce qu'il fait, et pourquoi.

La protection du CT interdit toute modification de disque, ajout d'un point de
montage compris. Il faut la lever puis la remettre :

```bash
pct set 200 --protection 0
pct set 200 --mp1 /root/homelab_proxmox/pve-eranikus/pgsql,mp=/etc/pgsql-git,ro=1
pct reboot 200                         # un mp n'est pris en compte qu'au démarrage
pct set 200 --protection 1
pct config 200 | grep -E 'protection|mp1'
```

Le script ne remet la protection que s'il l'a effectivement levée, et pose un
`trap` qui la rétablit même s'il est interrompu en cours de route. Un `Ctrl-C`
au mauvais moment ne laisse pas le conteneur déprotégé.

`pct reboot` rend la main **avant** que le CT ne soit utilisable : le script
attend ensuite que le conteneur soit `running` puis que `postgresql` soit
`active`, sinon la suite échouerait sur un montage pas encore visible.

Dans le CT, les fichiers apparaissent en `nobody:nogroup` : c'est le décalage
d'UID de 100000 propre aux conteneurs non privilégiés. Sans conséquence, les
fichiers étant en 644 et le montage en lecture seule.

## 4. Pose de la configuration

Les deux fichiers sont des **liens symboliques** vers le dépôt. PostgreSQL
accepte un symlink pour `pg_hba.conf` malgré ses exigences de permissions,
vérifié sur cette instance.

Posés par `pg-deploy.sh` (section 2). Ce qu'il fait :

```bash
ln -sf /etc/pgsql-git/10-homelab.conf /etc/postgresql/18/main/conf.d/10-homelab.conf
ln -sf /etc/pgsql-git/pg_hba.conf     /etc/postgresql/18/main/pg_hba.conf
systemctl restart postgresql           # listen_addresses exige un restart
```

La version du cluster n'est pas codée en dur dans le script : elle est lue dans
`pg_lsclusters`, et une installation qui en porterait plusieurs le fait
s'arrêter plutôt que d'en choisir un au hasard.

Le `restart` n'a lieu que si un symlink a réellement changé — `listen_addresses`
l'exige à la première pose, mais un `reload` suffit ensuite. `--restart` le
force.

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
pve-eranikus/pgsql/pg-deploy.sh
```

Le `git pull` suffit aux fichiers de configuration, qui sont des symlinks vers
le dépôt ; `pg-deploy.sh` recopie les scripts et les unités, qui sont des
copies, et applique le tout. Le `reload` de PostgreSQL est inconditionnel : les
symlinks ayant pu changer de contenu avec le `git pull` sans que le script
puisse s'en apercevoir, l'économiser ferait manquer un `pg_hba.conf` modifié.

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

`ss` doit montrer **deux** sockets : `0.0.0.0:5432` et `[::]:5432`. Un seul
socket, sur la boucle locale uniquement, est le symptôme d'une panne documentée
dans `docs/postgresql-listen-addresses-lxc.md` — service actif, base
injoignable.

## 5. Compte d'administration (`jbwittner`)

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

## 6. Ajout d'un locataire

Depuis l'intérieur du CT (`pct enter 200`) — le fichier `tenant.sql` est lu
dans le montage, et le socket Unix est le seul chemin en `peer` :

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
depuis le nœud `git pull` et `pg-deploy.sh` (section 2), qui applique le
rechargement. Côté client : `SSL_MODE = require`.

Dans les configurations applicatives, préférer un nom de domaine à l'IP — mais
le déclarer dans le `/etc/hosts` du CT client plutôt que de dépendre d'AdGuard,
sans quoi le service ne peut plus joindre sa base tant que le DNS n'est pas
debout.

## 7. Sauvegarde

Posée par `pg-deploy.sh` (section 2), qui fait dans le CT :

```bash
install -m 644 /etc/pgsql-git/pg-backup.service /etc/systemd/system/
install -m 644 /etc/pgsql-git/pg-backup.timer   /etc/systemd/system/
install -m 755 /etc/pgsql-git/pg-backup.sh      /usr/local/bin/pg-backup.sh
systemctl daemon-reload && systemctl enable --now pg-backup.timer
```

Puis, depuis l'hôte, pour une première exécution :

```bash
pgbk backup
```

L'unité pointe vers `/usr/local/bin/pg-backup.sh` : le script doit être copié,
pas lié, car le montage est en lecture seule et ne peut pas porter le bit
d'exécution. Chaque copie est comparée en contenu **et en mode** avant d'être
refaite, d'où un `pg-deploy.sh` sans effet quand rien n'a bougé.

Une exécution produit **un répertoire**, nommé par horodatage :

```
/var/backups/postgresql/
  20260819-233627/
    globals.sql          rôles et mots de passe (empreintes SCRAM)
    forgejo.dump         un dump -Fc par base
    MANIFEST             date, version PostgreSQL, liste des bases
  latest -> 20260819-233627
```

Restaurer, c'est prendre un répertoire : il contient un point cohérent dans le
temps. Rétention de 14 jours, `latest` pointe toujours vers la dernière.

Tout est écrit dans `<stamp>.part/` et renommé en `<stamp>/` seulement si
l'exécution va au bout. **Un répertoire présent est donc, par construction, une
sauvegarde complète** — une exécution interrompue ne laisse rien derrière elle.

### Journalisation

Sortie horodatée et préfixée par niveau (`STEP`, `INFO`, `WARN`, `ERROR`),
capturée par systemd :

```bash
journalctl -u pg-backup -n 50 --no-pager     # dernière exécution
journalctl -u pg-backup --since '7 days ago' # historique
journalctl -u pg-backup -p warning           # seulement les anomalies
```

Chaque exécution consigne la version PostgreSQL, l'inventaire des bases avec
leur taille brute, le besoin d'espace estimé, la durée et la taille de chaque
dump, ce que la purge a supprimé, et l'espace restant. L'objectif est de
pouvoir diagnostiquer une sauvegarde qui a mal tourné trois semaines plus tôt
sans avoir à rejouer le script.

En cas d'échec, le `trap` consigne la ligne fautive et le code de retour,
supprime le répertoire incomplet, et le dit explicitement — un échec silencieux
serait pire qu'une absence de sauvegarde.

### Contrôle d'espace

Les sauvegardes vivent sur `mp2`, un dataset ZFS de 50 Go pris sur le pool
`data` (NVMe 1 To) — donc sur un **disque physique distinct** de celui de la
base. Un incident sur le SSD 512 Go n'emporte plus les deux.

Le script refuse malgré tout de démarrer s'il ne peut pas garantir
`MIN_FREE_MB` libres à l'arrivée (512 Mo). L'enchaînement : estimation du
besoin à partir de `pg_database_size`, purge des répertoires expirés si la
marge est courte, nouveau contrôle, puis abandon avec un code d'erreur si
c'est toujours insuffisant — sans avoir rien écrit. Le contrôle est refait
avant chaque base, les premières ayant consommé de l'espace.

La purge n'a lieu qu'**en fin de course** : si la sauvegarde du jour échoue,
le script s'arrête avant, et les anciennes copies sont toujours là.

Le volume porte `backup=0`, donc les `vzdump` du CT ne l'embarquent pas.

**Le fichier globals est le plus facile à oublier et le plus coûteux à
perdre** : les rôles et leurs mots de passe ne figurent dans aucun `pg_dump` de
base. Sans lui, une restauration rend les données sans les comptes qui y
accèdent.

**Les deux disques sont dans la même machine.** La séparation protège d'une
panne de SSD, pas d'un vol, d'un incendie ou d'un `pct destroy` malencontreux —
qui emporterait le conteneur *et* son volume de sauvegardes. La copie hors-site
vers GCS reste le seul vrai filet — c'est `pgbk-offsite`, section 10.

## 8. `pgbk` — interface de gestion

`pg-backup.sh` est le moteur, appelé par le timer. `pgbk` est l'interface
humaine : il n'écrit aucune sauvegarde lui-même, il orchestre.

**Les commandes se tapent sur le nœud**, pas dans le CT :

```bash
pgbk backup                        # lance une sauvegarde via systemd
pgbk list                          # instantanés, âge, taille, bases
pgbk show 20260820-093240          # MANIFEST + fichiers
pgbk restore forgejo               # depuis le dernier instantané
pgbk restore forgejo 20260819      # depuis le plus récent de ce jour
pgbk verify forgejo                # contrôle ACL et propriétaires
pgbk delete 20260819-233627        # supprime une sauvegarde
```

### `pgbk delete` — et ce qu'il refuse

La rétention de `pg-backup.sh` fait le ménage courant ; `delete` est pour les
cas ponctuels — récupérer de la place, ou effacer un `pre-restore-*` une fois la
restauration validée (la rétention ne les purge pas).

**Le dernier instantané ne peut pas être supprimé.** Supprimer la dernière
sauvegarde laisserait le cluster sans filet, et c'est le genre d'erreur qu'on
ne remarque qu'au pire moment.

La garde ne porte pas sur le mot `latest` mais sur le **chemin résolu** : une
référence de la forme `AAAAMMJJ` désigne la plus récente de ce jour, qui est
souvent le dernier instantané sans jamais le nommer. Les trois formulations
sont donc refusées de la même façon :

```bash
pgbk delete latest              # refusé
pgbk delete 20260820-020000     # refusé si c'est la cible de latest
pgbk delete 20260820            # refusé — résout vers la même
```

Si le lien `latest` manque, la protection se reporte sur la plus récente : un
lien cassé ne doit pas ouvrir la porte. Sont également refusés les répertoires
`*.part` (exécution en cours ou interrompue, dont `pg-backup.sh` fait le ménage
lui-même) et tout chemin qui sortirait de `/var/backups/postgresql`.

Comme pour `restore`, il faut retaper le nom pour confirmer, et `--yes` court-
circuite la question. `--plan` dit ce qui serait supprimé sans rien effacer :

```bash
pgbk delete 20260819 --plan     # affiche la cible réelle et s'arrête
```

C'est ce que le nœud utilise pour poser sa question, le CT étant seul à savoir
à quel répertoire une référence correspond.

### Un seul fichier, deux rôles

`pgbk.sh` est posé **à l'identique** aux deux endroits par `pg-deploy.sh` :

| | |
|---|---|
| `/usr/local/sbin/pgbk` | sur le nœud |
| `/usr/local/bin/pgbk` | dans le CT |

Même contenu, même somme de contrôle. Il n'y a donc pas une « version hôte » et
une « version CT » à ne pas confondre au moment d'éditer : il n'existe qu'un
fichier dans le dépôt, et c'est l'endroit où il tourne qui décide de son
comportement.

La détection se fait sur la présence de `pct` — un nœud Proxmox l'a, le
conteneur Debian non :

- **sur le nœud** : il résout le CTID (section 2), vérifie que le conteneur
  tourne et que `pgbk` y est posé — un message qui renvoie vers `pg-deploy.sh`
  plutôt qu'un « command not found » —, pose la confirmation, puis `exec pct
  exec` et s'efface. Le code de retour est celui du CT.
- **dans le CT** : il fait le travail.

`--ctid <ID>` vise ponctuellement un autre conteneur sans toucher à
`/etc/default/pgbk`. `--local` force le mode moteur, utile pour déboguer.

### La confirmation est posée sur le nœud

`pct exec` n'alloue pas de TTY : un `read` exécuté dans le CT ne verrait jamais
la saisie, et la question de sécurité de `restore` serait muette. Elle est donc
posée du côté nœud, où le terminal existe, avant de déléguer avec `--yes`. Même
question, même réponse attendue ; seul l'endroit où le garde-fou est posé
change.

Appeler `pgbk` directement dans le CT (`pct enter 200`) reste possible : il y
est autonome, question comprise.

Un instantané se désigne par `latest`, une date `AAAAMMJJ` (le plus récent de
ce jour), ou un horodatage exact `AAAAMMJJ-HHMMSS`.

`pgbk backup` passe par `systemctl start`, donc avec le même environnement que
les exécutions du timer — pas de divergence entre lancement manuel et
automatique.

### Ce que fait `pgbk restore`

1. Capture le propriétaire **avant** le `dropdb` : il disparaît avec la base.
2. Demande de retaper le nom de la base (contournable par `--yes`).
3. **Dump de l'état courant** dans `pre-restore-<horodatage>/` — seule
   protection contre une erreur d'instantané.
4. Ferme les connexions, recrée la base, charge le dump avec `--role`.
5. Réapplique les ACL, que le dump ne contient pas.
6. Enchaîne sur `verify`.

Les répertoires `pre-restore-*` ne sont **pas** purgés par la rétention : ce
sont des filets, à supprimer à la main une fois la restauration validée.

### `pgbk verify`

Contrôle les deux pièges constatés lors du test de rollback du 20 août 2026 :
`PUBLIC` qui retrouve le droit `CONNECT`, et des tables appartenant à
`postgres` faute de `--role` au `pg_restore`. Les deux passent inaperçus sans
contrôle explicite — les données sont là, la base répond, et l'isolation a
disparu.

## 9. Restauration manuelle

```bash
# 1. Couper les connexions en cours, sinon dropdb échoue.
sudo -u postgres psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity \
     WHERE datname='forgejo' AND pid <> pg_backend_pid();"

# 2. Recréer la base vide. Les autres locataires restent en ligne.
sudo -u postgres dropdb forgejo
sudo -u postgres createdb forgejo -O forgejo -T template0 \
     --encoding UTF8 --lc-collate C --lc-ctype C

# 3. Restaurer les données.
sudo -u postgres pg_restore -d forgejo --no-owner --role=forgejo \
     /var/backups/postgresql/latest/forgejo.dump

# 4. RÉAPPLIQUER LES ACL — voir ci-dessous, l'étape la plus facile à oublier.
sudo -u postgres psql -v ON_ERROR_STOP=1 <<'SQL'
REVOKE CONNECT ON DATABASE forgejo FROM PUBLIC;
GRANT  CONNECT ON DATABASE forgejo TO forgejo;
SQL
```

### Les ACL ne sont pas dans le dump

**Vérifié le 20 août 2026 par un test de rollback complet.** Après
restauration, la colonne `Access privileges` de `\l forgejo` revient **vide**,
c'est-à-dire aux privilèges par défaut — donc `PUBLIC` a retrouvé le droit
`CONNECT`. L'isolation entre locataires a disparu, silencieusement.

Les droits au niveau base ne figurent ni dans un `pg_dump` sans `--create`, ni
dans `globals.sql`, qui ne porte que les rôles. L'étape 4 n'est donc pas
optionnelle.

Contrôle après restauration :

```bash
sudo -u postgres psql -c "\l forgejo"     # doit afficher =T/forgejo
sudo -u postgres psql -d forgejo -c "\dt" # Owner doit être forgejo, pas postgres
```

Le `--role=forgejo` de `pg_restore` est ce qui rétablit l'appartenance des
tables : le dump est pris en `--no-owner --no-acl`, l'appartenance n'y figure
pas. Sans ce drapeau, les tables appartiendraient à `postgres` et le service ne
pourrait plus écrire.

Reconstruction complète : recréer le cluster, rejouer le `globals.sql` du
répertoire choisi, puis ses dumps un par un, et réappliquer les ACL de chaque
locataire. Le `MANIFEST` rappelle la version PostgreSQL d'origine — un dump
produit en 18 ne se restaure pas sur une majeure antérieure.

**La protection du conteneur bloque la restauration d'un `vzdump` par-dessus le
CTID 200**, l'opération détruisant le CT avant de le recréer. Prévoir
`pct set 200 --protection 0` au préalable — c'est le second endroit où la
protection se met en travers, après l'ajout d'un point de montage.

## 10. Copie hors-site vers GCS — `pgbk-offsite`

Les deux disques du nœud sont dans la même machine (section 7). La séparation
SSD / NVMe protège d'une panne de disque, pas d'un vol, d'un incendie ou d'un
`pct destroy` malencontreux — qui emporterait le conteneur *et* son volume de
sauvegardes. **C'est cette copie-là qui est le vrai filet.**

Chaque nuit, l'hôte pousse vers Google Cloud Storage les répertoires
d'instantanés qui n'y sont pas encore :

```
gs://homelab-pgsql-backups-dc93212a/pve-eranikus/postgresql/20260820-093240/
    forgejo.dump
    globals.sql
    MANIFEST
```

Le **nœud est au premier niveau** : `vert-ysera` pourra s'ajouter en posant les
mêmes fichiers avec un autre `PGBK_OFFSITE_NODE`, sans rien restructurer.

### Le script tourne sur l'hôte, pas dans le CT

Décision délibérée. Le CT PostgreSQL est le composant le plus sensible du
nœud : il n'a aucune raison de détenir des identifiants GCP ni d'atteindre
internet. L'hôte lit directement le dataset ZFS — par sa **vue hôte**,
`/data/subvol-200-disk-0`, et non `/var/backups/postgresql` qui n'existe que
dans le conteneur — et mutualise `rclone` pour les futurs services du nœud.

Les dumps sont en `600`, propriété de `100102:100106` (décalage d'UID des CT
non privilégiés). Root sur l'hôte les lit sans difficulté ; aucun autre compte
de l'hôte ne le peut. D'où `User=root` dans l'unité.

### L'environnement GCS

| | |
|---|---|
| Bucket | `homelab-pgsql-backups-dc93212a` |
| Emplacement | `europe-west9` (Paris) |
| Cycle de vie | Nearline à 30 j, Coldline à 90 j, suppression à 365 j |
| Versionnement d'objet | désactivé |
| Accès uniforme au niveau bucket | **activé** — voir le piège ci-dessous |
| Compte de service | `roles/storage.objectViewer` + `roles/storage.objectCreator`, sur ce seul bucket |
| Client | `rclone` 1.60.1-DEV (paquet Debian trixie), `/usr/bin/rclone` |

**Le compte de service ne peut ni écraser ni supprimer.** L'écrasement exige
`objects.delete`, qui n'est pas accordé. C'est volontaire : un nœud compromis
ne doit pas pouvoir détruire l'historique distant. Les suppressions sont faites
côté serveur par la règle de cycle de vie, jamais par le nœud.

Trois conséquences, toutes visibles dans le code :

1. Le transfert se fait en `rclone copy --ignore-existing` : on ne *tente*
   jamais un écrasement, qui partirait en 403 à chaque exécution.
2. `rclone sync` est **interdit**. `sync` réplique les suppressions : un bug
   local, un dataset démonté, et la copie distante s'évapore avec l'originale.
   L'interdiction est structurante, pas cosmétique.
3. Un transfert interrompu peut laisser un objet partiel que **rien, sur ce
   nœud, ne pourra remplacer**. C'est le mode de panne le plus probable de tout
   le montage — voir « Objet distant divergent » plus bas.

> Cette protection contre les suppressions se vérifie **par lecture du code et
> des droits IAM**. Ne jamais la « tester » en vidant le répertoire local pour
> voir si le distant suit : le seul résultat garanti d'un tel test est la perte
> des sauvegardes locales.

### Prérequis sur le nœud

`rclone` et la configuration du remote, à faire une fois :

```bash
apt install rclone                      # 1.60.1-DEV sur trixie, suffisant

install -d -m 700 /root/.config/rclone
cat > /root/.config/rclone/rclone.conf <<'EOF'
[gcs]
type = google cloud storage
service_account_file = /root/.config/rclone/pgsql-backups.json
bucket_policy_only = true
EOF
chmod 600 /root/.config/rclone/rclone.conf
```

Le listage prouve la clé, le réseau et les droits de lecture — **pas** que
l'écriture passe. Le seul test d'écriture est la première exécution de
`pgbk-offsite` : ne pas déposer d'objet-sonde dans le bucket, le nœud n'aurait
pas le droit de l'effacer et il y resterait un an.

La **clé JSON du compte de service** se dépose en
`/root/.config/rclone/pgsql-backups.json`, en `600`. Elle vient du gestionnaire
de secrets (OpenBao) : **elle n'est pas dans ce dépôt et n'y entrera pas**. Le
`.gitignore` de la racine refuse les fichiers de clé par précaution.

```bash
chmod 600 /root/.config/rclone/pgsql-backups.json
rclone --config /root/.config/rclone/rclone.conf \
       lsf gcs:homelab-pgsql-backups-dc93212a     # doit répondre sans erreur
```

#### Le piège de l'accès uniforme (UBLA)

Le bucket est en **uniform bucket-level access** : les droits viennent
entièrement de l'IAM, et les ACL par objet sont refusées. Or `rclone` joint par
défaut une ACL héritée (`predefinedAcl`) à chaque insertion. Résultat, une
erreur par fichier et **aucun objet écrit** :

```
ERROR : forgejo.dump: Failed to copy: googleapi: Error 400: Cannot insert legacy
ACL for an object when uniform bucket-level access is enabled, invalid
```

L'insertion étant rejetée avant tout stockage, ces échecs ne laissent **pas**
d'objet partiel : il n'y a rien à nettoyer, seulement à relancer une fois la
configuration corrigée.

Deux endroits le règlent, et ils ne se gênent pas :

- `bucket_policy_only = true` dans `rclone.conf`, ci-dessus — vaut aussi pour
  les appels `rclone` faits à la main ;
- `--gcs-bucket-policy-only` dans `pgbk-offsite.sh`, pour que le script
  fonctionne même sur une configuration reconstruite à la va-vite.

Constaté le 20 août 2026, à la première exécution réelle.

### Installation

C'est `pg-deploy.sh` qui pose l'ensemble, comme pour le reste du CT — **une
fois les prérequis ci-dessus réunis** :

```bash
cd /root/homelab_proxmox && git pull
pve-eranikus/pgsql/pg-deploy.sh
```

Sa section « Copie hors-site vers GCS (sur l'hote) » fait, sur **l'hôte** et
non dans le CT :

```bash
install -m 755 pgbk-offsite.sh      /usr/local/bin/pgbk-offsite
install -m 644 pgbk-offsite.service /etc/systemd/system/pgbk-offsite.service
install -m 644 pgbk-offsite.timer   /etc/systemd/system/pgbk-offsite.timer
systemctl daemon-reload
systemctl enable --now pgbk-offsite.timer
```

Comme pour les fichiers du CT, le script est **copié et non lié** : le dépôt
peut être déplacé, ou en cours de `git pull` à 3h30. Modifier `pgbk-offsite.sh`
dans le dépôt ne change donc rien tant que `pg-deploy.sh` n'a pas été rejoué.

**Le timer n'est armé que si `rclone` et la clé sont là.** Sans eux, le script
et les unités sont quand même posés — ce sont des fichiers inertes, et les
avoir en place permet un `pgbk-offsite --dry-run` de diagnostic — mais le timer
reste inactif et le résumé affiche `KO pgbk-offsite.timer (inactive)`. Armer un
timer qui échouera toutes les nuits à 3h30 n'aiderait personne. Réunir les
prérequis, rejouer `pg-deploy.sh`.

Deux autres refus, visibles dans le résumé :

| Message | Cause |
|---|---|
| `pgbk-offsite (source /data/subvol-200-disk-0 absente)` | le CT est à l'arrêt : le dataset n'est monté côté hôte que quand il tourne. Le timer reste armé, il n'y a rien à corriger |
| `pgbk-offsite (source hors CT 201)` | `--ctid 201` avec une unité qui pointe toujours sur le volume du 200. **Le timer n'est pas armé** : une copie verte tous les soirs sur les sauvegardes du mauvais conteneur serait pire qu'une copie absente |

`pg-deploy.sh --no-offsite` saute entièrement cette section — pour un nœud sans
copie hors-site, ou pour ne toucher qu'au CT.

Le script relit `pgbk-offsite.service` pour connaître `PGBK_OFFSITE_RCLONE`,
`PGBK_OFFSITE_KEY` et `PGBK_OFFSITE_SRC` : l'unité reste le seul endroit où ces
chemins sont écrits.

### Vérification

```bash
/usr/local/bin/pgbk-offsite --dry-run     # annonce, n'envoie rien
systemctl start pgbk-offsite.service      # première exécution réelle
journalctl -u pgbk-offsite -n 60 --no-pager
```

Ce qu'il faut observer pour conclure que ça marche :

1. La première exécution transfère **tous** les instantanés locaux et se
   termine par `terminé en Ns — N instantané(s) en ligne`.
2. La seconde, lancée dans la foulée, ne transfère **rien** :
   `bilan — 0 transféré(s), N déjà en ligne, 0 en échec, 0 divergent(s)`.
   C'est le contrôle d'idempotence — s'il retransfère, quelque chose ne va pas.
3. Le timer est armé :

```bash
systemctl list-timers pgbk-offsite.timer    # prochaine échéance à 3h30 (+ délai aléatoire)
```

4. Le contenu distant correspond à `pgbk list` :

```bash
rclone --config /root/.config/rclone/rclone.conf \
       lsf gcs:homelab-pgsql-backups-dc93212a/pve-eranikus/postgresql/
```

Les codes de retour sont faits pour être exploités par une supervision :

| Code | Sens |
|---|---|
| 0 | tout est en ligne |
| 1 | environnement inutilisable : `rclone`, clé, bucket, ou aucune sauvegarde locale |
| 2 | au moins un transfert a échoué |
| 3 | au moins un objet distant diverge de sa source — intervention humaine |

Un échec est bruyant par construction : `journalctl -u pgbk-offsite -p warning`
ne doit rien afficher pour une nuit normale. Le script signale aussi en `WARN`
un dernier instantané local vieux de plus de 48 h — une copie hors-site
parfaitement verte au-dessus d'une sauvegarde locale à l'arrêt ne protège plus
rien.

### Ce qui n'est jamais transféré

| Écarté | Pourquoi |
|---|---|
| `latest` | symlink **absolu** vers `/var/backups/postgresql/...`, chemin qui n'existe que dans le CT — donc cassé vu de l'hôte |
| `pre-restore-*` | filets posés par `pgbk restore` avant d'écraser une base : locaux, temporaires, sans valeur distante |
| `*.part` | exécution en cours ou interrompue. Par construction de `pg-backup.sh`, un répertoire **sans** ce suffixe est complet |

Le timer est à **3h30**, une heure après la sauvegarde locale du CT (2h30) :
assez pour qu'elle soit terminée et son répertoire renommé.

### Objet distant divergent — le cas à traiter à la main

Après chaque transfert, le script relance un `rclone check --one-way` sur
**tout** l'instantané, pas seulement sur ce qui vient de partir. C'est là, et
nulle part ailleurs, qu'un objet partiel laissé par une exécution coupée se
révèle :

```
[ERROR]   20260819-234306 : le distant DIVERGE de la source
          ERROR : forgejo.dump: md5 differ
[ERROR]   ces objets ne peuvent pas être corrigés depuis ce nœud : le compte de
[ERROR]   service n'a pas le droit d'écraser (objectCreator sans objects.delete).
```

Le script **ne tente pas de réparer**, et surtout pas en boucle : une reprise
qui se heurte à un 403 toutes les nuits masquerait le problème au lieu de le
montrer. La correction demande le compte personnel, depuis un poste
d'administration :

```bash
gcloud auth login
gcloud storage rm gs://homelab-pgsql-backups-dc93212a/pve-eranikus/postgresql/20260819-234306/forgejo.dump
```

Puis, sur le nœud :

```bash
systemctl start pgbk-offsite.service    # l'objet manquant est renvoyé, puis contrôlé
```

### Restauration depuis GCS

**La récupération se fait avec le compte personnel, pas avec la clé du nœud.**
C'est délibéré : la restauration ne doit dépendre d'aucun secret stocké sur le
nœud, sinon elle échoue précisément dans le scénario où l'on en a besoin — nœud
détruit, volé, ou compte de service révoqué. Le compte de service, de son côté,
n'a de toute façon pas le droit d'écrire ailleurs que dans ce bucket.

**1. Récupérer un instantané, depuis n'importe quel poste :**

```bash
gcloud auth login
gcloud storage ls gs://homelab-pgsql-backups-dc93212a/pve-eranikus/postgresql/
gcloud storage cp -r \
  gs://homelab-pgsql-backups-dc93212a/pve-eranikus/postgresql/20260820-093240 .
cat 20260820-093240/MANIFEST     # date, version PostgreSQL, bases contenues
```

Le `MANIFEST` est à lire **avant** de restaurer : un dump produit en
PostgreSQL 18 ne se restaure pas sur une majeure antérieure.

**2. Remonter l'instantané dans le CT**, via le nœud :

```bash
scp -r 20260820-093240 root@192.168.1.11:/tmp/

# puis, sur le nœud — pct push prend un fichier à la fois, d'où la boucle
pct exec 200 -- mkdir -p /var/backups/postgresql/20260820-093240
for f in /tmp/20260820-093240/*; do
  pct push 200 "$f" "/var/backups/postgresql/20260820-093240/$(basename "$f")"
done

# les fichiers arrivent en root:root ; pg_restore tourne en postgres
pct exec 200 -- chown -R postgres:postgres /var/backups/postgresql/20260820-093240
pct exec 200 -- chmod 700 /var/backups/postgresql/20260820-093240
```

**3. Restaurer normalement**, l'instantané est redevenu un instantané local
comme les autres :

```bash
pgbk show    20260820-093240      # contrôle : MANIFEST et fichiers attendus
pgbk restore forgejo 20260820-093240
pgbk verify  forgejo
```

`pgbk restore` prend au passage un filet `pre-restore-*` de l'état courant, et
réapplique les ACL — l'étape que la restauration manuelle de la section 9
oublie le plus souvent.

**Reconstruction complète** (cluster perdu) : récupérer le répertoire, rejouer
d'abord `globals.sql` — les rôles et leurs mots de passe ne sont dans aucun
`pg_dump` de base —, puis les dumps un par un, puis les ACL de chaque
locataire. `pgbk restore` refuse de restaurer une base dont le rôle
propriétaire n'existe pas : c'est le rappel que `globals.sql` passe en premier.

**`globals.sql` contient les empreintes SCRAM de tous les rôles.** C'est le
fichier le plus sensible du lot, et il part hors-site. Le bucket est privé,
chiffré au repos par GCS, et son IAM se limite à ce compte de service plus les
comptes personnels d'administration ; il n'y a rien de plus à en attendre. Le
récupérer sur un poste, c'est y poser des empreintes de mots de passe — les
effacer une fois la restauration finie.

### Paramétrage

Valeurs par défaut dans le script, valeurs réelles dans
`pgbk-offsite.service` : l'unité est le seul endroit qui décrit ce nœud-ci.

| Variable | Défaut | Rôle |
|---|---|---|
| `PGBK_OFFSITE_NODE` | `$(hostname -s)` | premier niveau distant — `pve-eranikus` |
| `PGBK_OFFSITE_SRC` | `/data/subvol-200-disk-0` | **vue hôte** du dataset de sauvegarde |
| `PGBK_OFFSITE_REMOTE` | `gcs` | remote déclaré dans `rclone.conf` |
| `PGBK_OFFSITE_BUCKET` | `homelab-pgsql-backups-dc93212a` | |
| `PGBK_OFFSITE_SUBPATH` | `postgresql` | second niveau distant, sous le nœud |
| `PGBK_OFFSITE_CONFIG` | `/root/.config/rclone/rclone.conf` | explicite : sous systemd, `HOME` n'est pas garanti |
| `PGBK_OFFSITE_KEY` | `/root/.config/rclone/pgsql-backups.json` | contrôlée avant tout transfert |
| `PGBK_OFFSITE_RCLONE` | `/usr/bin/rclone` | chemin absolu, le `PATH` systemd est minimal |
| `PGBK_OFFSITE_TRANSFERS` | `4` | transferts parallèles |
| `PGBK_OFFSITE_RETRIES` | `3` | reprises `rclone` |
| `PGBK_OFFSITE_BWLIMIT` | *(vide)* | bridage, ex. `10M` pour épargner la montée ADSL |
| `PGBK_OFFSITE_CHECK` | `hash` | `size` pour un contrôle plus rapide et plus faible |
| `PGBK_OFFSITE_STALE_HOURS` | `48` | âge du dernier instantané local au-delà duquel on alerte |

## Reste à faire

- [x] Installer le timer de sauvegarde (section 7).
- [ ] Ligne du locataire `forgejo` — dépend de son IP définitive.
- [x] Copie hors-site des dumps vers GCS (section 10).
- [x] Faire poser `pgbk-offsite` et ses unités par `pg-deploy.sh`
      (section E du script).
- [ ] Copier `postgresql.vars` dans ce dépôt après vérification des secrets.
- [x] Hook post-install (`pct set`, montage, symlinks, timer) pour rendre
      l'ensemble rejouable — c'est `pg-deploy.sh` (section 2), écrit à partir
      des commandes qui avaient réellement fonctionné à la main.

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
- `listen_addresses = '*'` est délibéré, pas un oubli de durcissement. Une IP
  explicite crée une course au démarrage dans un LXC : PostgreSQL peut démarrer
  avant que `eth0` ne porte l'adresse, n'ouvrir que le socket loopback, et se
  déclarer actif malgré tout. Le CT n'ayant qu'une interface, `'*'` couvre
  exactement les mêmes adresses. Le contrôle d'accès est dans `pg_hba.conf`.
  Détail complet dans `docs/postgresql-listen-addresses-lxc.md`.
- **Ne jamais ajouter `After=network-online.target`** à l'unité PostgreSQL dans
  un LXC : la cible n'est jamais atteinte et le service reste indéfiniment en
  attente (`Active: inactive (dead)` avec un `Job:` en file).
- `work_mem` est **par nœud de tri et par connexion**. À 100 connexions et
  8 Mo, le pire cas théorique dépasse la RAM du CT : surveiller
  `log_temp_files` plutôt que d'augmenter à l'aveugle.