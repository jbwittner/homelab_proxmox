# CT PostgreSQL mutualisé — `pve-eranikus`

Cluster PostgreSQL unique servant les services LXC du nœud. Un couple
base + rôle par locataire, isolés les uns des autres.

Déployé le 19 août 2026 via le script communautaire `postgresql.sh`.
Le CTID appartient à la plage **200-299**, réservée par convention aux
services installés depuis un script communautaire : l'installation n'est
pas décrite dans ce dépôt, elle se rejoue en relançant le script.

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

## 1. Création du conteneur

```bash
var_os='debian' bash -c "$(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/postgresql.sh)"
```

Réponses au questionnaire, sauvegardées par le script dans
`/usr/local/community-scripts/defaults/postgresql.vars` — à copier ici
après avoir vérifié qu'aucun secret n'y figure (`grep -i pass`).

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
| FUSE / TUN / mknod / mount FS | non | rien de tout cela n'est utile à un démon PostgreSQL |
| **Nesting** | **oui** | **obligatoire sur Debian 13** — voir ci-dessous |
| Protection | oui | ce CT porte les données de tous les services |
| Timezone | Europe/Paris | concorde avec `timezone` du drop-in |
| APT Cacher / proxy | non | |
| Post-install hook | vide | à écrire plus tard (voir « Reste à faire ») |
| Adminer | non | interface web PHP non suivie, sur le CT le plus sensible |

### Le piège du nesting

Le script demande le nesting **avant** d'afficher l'avertissement qui explique
pourquoi il le faut, et la réponse n'est pas rattrapable ensuite : il faut tout
recommencer. Réponds **oui** directement.

Depuis systemd 254, les unités utilisent le mécanisme de *credentials*, qui
exige de monter un tmpfs — impossible pour un CT non privilégié avec le profil
AppArmor standard, d'où l'erreur `243/CREDENTIALS` et un conteneur qui démarre
en état dégradé. `nesting=1` bascule sur le profil
`lxc-container-default-nesting` qui l'autorise. Cela concerne aussi les
directives `PrivateTmp` et `NoNewPrivileges` de `pg-backup.service`.

### Après création

```bash
pct set 200 --startup order=1          # PostgreSQL avant ses locataires
pct config 200 | grep -E 'net0|features|protection'
pct exec 200 -- systemctl status fstrim.timer   # indispensable sur LVM-thin
```

Le stockage étant du **LVM-thin** et non du ZFS, aucun réglage de `recordsize`
n'est nécessaire. Deux conséquences en revanche : le pool est surprovisionné
(surveiller `lvs`, un pool saturé arrête net le serveur), et
`full_page_writes` doit rester à `on` — ext4 sur LVM n'offre aucune garantie
d'atomicité des écritures de page.

## 2. Configuration

```bash
pct set 200 --mp1 /srv/homelab_proxmox/eranikus/pgsql,mp=/opt/homelab/pgsql,ro=1
pct enter 200
```

```bash
# Drop-in : postgresql.conf n'est jamais modifié, donc aucun .dpkg-dist
# à arbitrer lors des mises à jour du paquet.
ln -sf /opt/homelab/pgsql/conf.d/10-homelab.conf \
       /etc/postgresql/18/main/conf.d/10-homelab.conf

# pg_hba.conf ne supporte pas l'inclusion de façon fiable : on copie.
install -o postgres -g postgres -m 640 \
        /opt/homelab/pgsql/pg_hba.conf /etc/postgresql/18/main/pg_hba.conf

systemctl restart postgresql    # listen_addresses exige un restart, pas un reload
```

Vérifications :

```bash
sudo -u postgres psql -c "SHOW listen_addresses; SHOW ssl; SHOW shared_buffers; SHOW jit;"
ss -lntp | grep 5432
pg_lsclusters
df -h /dev/shm                  # < 256 Mo bloquerait les requêtes parallèles
```

**Si `SHOW ssl` renvoie `off`** : `apt install ssl-cert`, puis pointer
`ssl_cert_file` et `ssl_key_file` vers `/etc/ssl/certs/ssl-cert-snakeoil.pem`
et sa clé. Tant que ce n'est pas fait, les règles doivent rester en `host` et
non `hostssl`, sinon les connexions sont refusées sans message explicite.

## 3. Compte d'administration (`jbwittner`)

Créé depuis l'intérieur du CT, en peer sur socket Unix :

```bash
pct enter 200
PASS="$(head -c 32 /dev/urandom | base64 | tr -d '\n=+/')"
sudo -u postgres psql -c "CREATE ROLE jbwittner LOGIN SUPERUSER PASSWORD '${PASS}';"
echo "$PASS"      # → OpenBao immédiatement, il ne réapparaîtra pas
```

Mot de passe perdu ? Aucun blocage possible tant que la ligne `local all
postgres peer` existe :

```bash
sudo -u postgres psql -c "ALTER ROLE jbwittner PASSWORD '<nouveau>';"
```

La ligne d'autorisation correspondante — le nom du rôle doit correspondre
**exactement**, sinon aucune règle ne matche et la connexion est refusée :

```
host      all    jbwittner    192.168.1.11/32    scram-sha-256
```

### Connexion depuis le Mac

Deux chemins, celui du tunnel est préférable : un poste mobile n'a rien à
faire dans un fichier qui décrit l'infrastructure, et le tunnel fonctionne
encore depuis l'extérieur.

```bash
ssh -L 5432:192.168.1.56:5432 root@192.168.1.11
```

DBeaver : hôte `localhost`, port `5432`, base `postgres`, utilisateur
`jbwittner`, SSL `require`. DBeaver sait monter le tunnel lui-même — onglet
*SSH*, hôte `192.168.1.11`. **Le champ *Host* de la connexion principale doit
alors contenir `192.168.1.56`**, c'est-à-dire l'adresse vue depuis le nœud une
fois le tunnel établi ; y mettre `localhost` cherche PostgreSQL sur le nœud
Proxmox lui-même et produit un « Connection reset ».

En accès direct depuis le LAN, remplacer `192.168.1.11/32` par l'IP fixe du
Mac.

Le `SUPERUSER` contourne le `REVOKE CONNECT` posé sur chaque base de
locataire : pratique pour administrer, mais à ne pas utiliser comme
identifiant de consultation courante — une requête maladroite sur la base d'un
service passera sans garde-fou.

## 4. Ajout d'un locataire

```bash
PASS="$(head -c 32 /dev/urandom | base64 | tr -d '\n=+/')"
sed -e 's/@@NAME@@/monservice/g' -e "s|@@PASSWORD@@|${PASS}|" \
    /opt/homelab/pgsql/sql/tenant.sql.tpl \
  | sudo -u postgres psql -v ON_ERROR_STOP=1
echo "$PASS"
```

Ajouter la ligne correspondante dans `pg_hba.conf`, **avant** le `reject`, puis
`systemctl reload postgresql`. Côté client : `SSL_MODE = require` (certificat
auto-signé, donc pas `verify-full`).

Dans les configurations applicatives, préférer un nom de domaine à l'IP — mais
le déclarer dans le `/etc/hosts` du CT client plutôt que de dépendre d'AdGuard,
sans quoi le service ne peut plus joindre sa base tant que le DNS n'est pas
debout.

## 5. Sauvegarde

```bash
install -m 644 /opt/homelab/pgsql/systemd/pg-backup.service /etc/systemd/system/
install -m 644 /opt/homelab/pgsql/systemd/pg-backup.timer   /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now pg-backup.timer
systemctl start pg-backup.service && journalctl -u pg-backup -n 20
```

Un `.dump` par base plus un `globals-*.sql` par exécution, 14 jours de
rétention dans `/var/backups/postgresql`.

**Le fichier globals est le plus facile à oublier et le plus coûteux à
perdre** : les rôles et leurs mots de passe ne figurent dans aucun `pg_dump` de
base. Sans lui, une restauration rend les données sans les comptes qui y
accèdent.

**Ces dumps vivent sur le même disque que la base qu'ils protègent.** Deux
copies sur le même SSD ne survivent pas à une panne matérielle : c'est le
`vzdump` et la copie hors-site qui couvrent ce risque, pas ce timer.

## 6. Restauration

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
CTID 200**, puisque l'opération détruit le CT avant de le recréer. Prévoir
`pct set 200 --protection 0` au préalable.

## Reste à faire

- [ ] Resserrer `pg_hba.conf` : vérifier qu'aucune règle large (`0.0.0.0/0`
      ou `/24`) n'a été laissée par le script, et poser les `/32`.
- [ ] Poser le drop-in ; `listen_addresses` est actuellement à `0.0.0.0`.
- [ ] Vérifier `SHOW ssl`, puis durcir les règles en `hostssl`.
- [ ] Ligne du locataire `forgejo` — dépend de son IP définitive.
- [ ] Copie hors-site des dumps vers GCS.
- [ ] Hook post-install (`pct set`, bind-mount, pose de la conf, timer) pour
      rendre l'ensemble rejouable. À écrire à partir de ce qui a réellement
      fonctionné, pas avant.

## Notes

- Ce CT est un point de défaillance unique pour tous les services du nœud.
  C'est le prix assumé de la mutualisation ; une montée de version majeure
  devient une fenêtre de maintenance pour tout le monde.
- Montée de majeure : `pg_upgradecluster` (outillage `postgresql-common`,
  spécifique Debian), après snapshot du CT. PostgreSQL 18 conserve les
  statistiques du planner à la migration, il n'y a plus d'`ANALYZE` massif à
  lancer ensuite.
- `work_mem` est **par nœud de tri et par connexion**. À 100 connexions et
  8 Mo, le pire cas théorique dépasse la RAM du CT : surveiller
  `log_temp_files` plutôt que d'augmenter à l'aveugle.
- PostgreSQL 18 est supporté jusqu'en novembre 2030.
