# PRA — Plan de reprise, CT PostgreSQL `pve-eranikus`

Quoi faire quand quelque chose est perdu. **Un scénario, une procédure.**

Les gestes courants sont dans [README.md](../README.md), le détail de chaque
composant dans [RUNBOOK.md](RUNBOOK.md). Ce fichier-ci ne sert que les mauvais
jours : il commence par le diagnostic et ne suppose rien d'acquis.

**Ce document se répète volontairement.** En reprise, on ne lit pas un
document en entier — on va à son scénario et on doit y trouver tout ce qu'il
faut, sans naviguer.

La procédure de test de ce plan est dans [PRA-exercice.md](PRA-exercice.md).
Un PRA qui n'a jamais été joué n'est pas un plan, c'est une intention.

## Ce sur quoi on peut compter

| | |
|---|---|
| Sauvegarde locale | tous les jours à 2h30, rétention **14 jours**, sur `mp2` — un volume NVMe distinct du SSD qui porte la base |
| Copie hors-site | tous les jours à 3h30 vers `gs://homelab-pgsql-backups-dc93212a/pve-eranikus/postgresql/`, conservée **365 jours** |
| Contenu d'un instantané | un `<base>.dump` par base, `globals.sql` (rôles **et empreintes de mots de passe**), `MANIFEST` (date, version PostgreSQL, bases) |
| Secrets | OpenBao — mais `globals.sql` suffit à rendre leurs accès aux services, voir scénario 6 |
| Reconstruction du service | `pg deploy`, une commande |

**RPO — au pire 24 h**, l'écart entre deux sauvegardes. Il n'y a pas
d'archivage WAL : ce qui a été écrit depuis la dernière sauvegarde est perdu.
C'est un choix assumé pour un homelab ; le dire ici évite de le découvrir en
pleine reprise.

**RTO — non mesuré tant que l'exercice n'a pas été joué.** Les durées de
chaque scénario sont à remplir depuis [PRA-exercice.md](PRA-exercice.md), pas
à estimer de tête.

## Trouver son scénario

| Ce qu'on constate | Scénario |
|---|---|
| Une base est vide, corrompue, ou un `DELETE` est parti trop loin | [1 — une base](#1--une-base-perdue-ou-corrompue) |
| PostgreSQL ne démarre plus, le CT et le volume sont là | [2 — le cluster](#2--le-cluster-ne-démarre-plus) |
| Le CT 200 n'existe plus, le nœud fonctionne | [3 — le conteneur](#3--le-conteneur-est-détruit) |
| Le nœud est perdu : panne totale, vol, incendie | [4 — le nœud](#4--le-nœud-est-perdu) |
| Le nœud a été compromis | [5 — compromission](#5--compromission-du-nœud) |
| Les mots de passe des services sont perdus | [6 — les secrets](#6--les-secrets-sont-perdus) |
| Un dump refuse de se restaurer | [7 — un instantané illisible](#7--un-instantané-est-illisible) |

## Avant toute chose

**Ne pas se précipiter sur une restauration.** Deux gestes d'abord, dans cet
ordre :

```bash
# 1. Arrêter les services qui écrivent dans la base concernée : une
#    restauration par-dessus une application vivante recrée le problème.

# 2. Regarder ce qu'on a, avant de toucher quoi que ce soit.
pgbk list                                   # instantanés locaux
rclone --config /root/.config/rclone/rclone.conf \
       lsf gcs:homelab-pgsql-backups-dc93212a/pve-eranikus/postgresql/
```

`pgbk restore` prend un filet `pre-restore-*` de l'état courant avant
d'écraser. C'est la seule protection contre « je me suis trompé
d'instantané » — ne pas la contourner, et ne pas purger ces répertoires tant
que la reprise n'est pas validée.

## 1 — Une base perdue ou corrompue

**Ce qui reste** : le CT, le cluster, les autres locataires, tous les
instantanés locaux.

**Portée** : un seul locataire. Les autres bases restent en ligne pendant
l'opération.

```bash
pgbk list                                 # choisir l'instantané
pgbk show 20260820-093240                 # vérifier qu'il contient la base
pgbk restore forgejo 20260820-093240      # demande de retaper le nom
pgbk verify forgejo
```

`pgbk restore` ferme les connexions, prend le filet `pre-restore-*`, recrée la
base, charge le dump avec `--role`, **réapplique les ACL** et enchaîne sur
`verify`. Détail : [runbook § 8](RUNBOOK.md#ce-que-fait-pgbk-restore).

**Contrôles avant de rouvrir le service** :

```bash
pgbk verify forgejo      # ACL et propriétaires des tables
```

`verify` doit montrer une ACL non vide et « propriétaire des tables : OK ».
Une ACL vide veut dire que `PUBLIC` a retrouvé `CONNECT` et que l'isolation
entre locataires a disparu — silencieusement
([runbook § 9](RUNBOOK.md#les-acl-ne-sont-pas-dans-le-dump)).

Redémarrer ensuite le service applicatif, et vérifier qu'il écrit.

## 2 — Le cluster ne démarre plus

**Ce qui reste** : le CT, `mp2` et ses instantanés.

**Diagnostic d'abord** — une configuration invalide se corrige en deux
minutes, un cluster à reconstruire prend une heure :

```bash
pct exec 200 -- systemctl status postgresql
pct exec 200 -- journalctl -u postgresql -n 50 --no-pager
pct exec 200 -- pg_lsclusters
```

**Si c'est la configuration** (le cas le plus fréquent : un `log_connections`
booléen, un `listen_addresses` malheureux, un drop-in cassé) — le dépôt est la
référence, il suffit de la reposer :

```bash
cd /root/homelab_proxmox && git pull
pve-eranikus/pgsql/pg deploy --restart
```

Penser à `postgresql.auto.conf` (`/var/lib/postgresql/18/main/`), écrit par les
`ALTER SYSTEM SET` : il est lu **en dernier** et écrase le drop-in du dépôt.
`ALTER SYSTEM RESET <param>;` pour le nettoyer.

**Si le cluster est irrécupérable**, le reconstruire et recharger le dernier
instantané. Les instantanés sont sur `mp2`, ils ne sont pas concernés :

```bash
pct enter 200
pg_dropcluster --stop 18 main
pg_createcluster 18 main --start -- --auth-local peer --auth-host scram-sha-256 \
    --locale C.UTF-8

# 1. les rôles D'ABORD : aucune base ne peut appartenir à un rôle absent
sudo -u postgres psql -f /var/backups/postgresql/latest/globals.sql

# 2. puis chaque base
sudo -u postgres createdb forgejo -O forgejo -T template0 \
     --encoding UTF8 --lc-collate C --lc-ctype C
sudo -u postgres pg_restore -d forgejo --no-owner --role=forgejo \
     /var/backups/postgresql/latest/forgejo.dump

# 3. et les ACL, que le dump ne contient pas
sudo -u postgres psql -v ON_ERROR_STOP=1 <<'SQL'
REVOKE CONNECT ON DATABASE forgejo FROM PUBLIC;
GRANT  CONNECT ON DATABASE forgejo TO forgejo;
SQL
```

Puis reposer la configuration depuis le nœud, qui remet les symlinks, les
unités et le timer :

```bash
pve-eranikus/pgsql/pg deploy
```

## 3 — Le conteneur est détruit

**Ce qui a disparu avec lui : les sauvegardes locales.** `mp2` est un volume
du CT 200 ; `pct destroy` emporte le conteneur *et* ses volumes. C'est
précisément le scénario pour lequel la copie hors-site existe.

**Ce qui reste** : le nœud, le dépôt git, la clé de service, et l'intégralité
de l'historique dans GCS.

**Si un `vzdump` du CT existe**, c'est le chemin le plus court — mais il ne
contient **pas** les dumps (`mp2` porte `backup=0`) : il rend le conteneur et
sa base telle qu'elle était au moment du vzdump.

```bash
pct set 200 --protection 0        # la protection bloque la restauration d'un vzdump
pct restore 200 /var/lib/vz/dump/<archive>.tar.zst
pve-eranikus/pgsql/pg deploy   # repose mp1, mp2, config, timers, protection
```

**Sinon, reconstruction complète** — le conteneur d'abord, les données
ensuite :

```bash
# 1. Recréer le CT avec le script communautaire (runbook § 1), CTID 200,
#    mêmes réponses au questionnaire — nesting=oui.

# 2. Tout reposer, d'une commande. Elle crée mp2, la configuration, les
#    unités, et déclenche une première sauvegarde (vide, sans importance).
cd /root/homelab_proxmox && git pull
pve-eranikus/pgsql/pg deploy
```

Puis récupérer le dernier instantané depuis GCS — **avec le compte personnel**,
voir [scénario 4](#4--le-nœud-est-perdu) pour la commande exacte —, le pousser
dans le CT et restaurer :

```bash
pgbk show    20260820-093240
pgbk restore forgejo 20260820-093240
pgbk verify  forgejo
```

Les rôles n'existent pas encore dans un cluster neuf : rejouer `globals.sql`
**avant** le premier `pgbk restore`, sans quoi il refuse (le rôle propriétaire
manque).

```bash
pct exec 200 -- sudo -u postgres psql -f /var/backups/postgresql/20260820-093240/globals.sql
```

## 4 — Le nœud est perdu

Panne matérielle totale, vol, incendie. **Rien de local n'est récupérable.**

**Ce qui reste** : le dépôt git (poussé sur Forgejo ou GitHub), OpenBao, et le
bucket GCS. Rien d'autre n'est nécessaire.

**La récupération se fait avec le compte personnel, pas avec la clé du nœud** —
elle a disparu avec lui, et c'est le principe : la restauration ne doit
dépendre d'aucun secret stocké sur la machine perdue.

```bash
# depuis n'importe quel poste
gcloud auth login
gcloud storage ls gs://homelab-pgsql-backups-dc93212a/pve-eranikus/postgresql/
gcloud storage cp -r \
  gs://homelab-pgsql-backups-dc93212a/pve-eranikus/postgresql/20260820-093240 .
cat 20260820-093240/MANIFEST
```

**Lire le `MANIFEST` avant de reconstruire** : il donne la version PostgreSQL
d'origine. Un dump produit en 18 ne se restaure pas sur une majeure
antérieure — la machine de remplacement doit porter au moins la même.

Ensuite, sur le nœud de remplacement (`vert-ysera`, ou du matériel neuf) :

```bash
# 1. Cloner le dépôt.
git clone <url> /root/homelab_proxmox

# 2. Créer le CT avec le script communautaire (runbook § 1).

# 3. Tout reposer.
/root/homelab_proxmox/pve-eranikus/pgsql/pg deploy

# 4. Pousser l'instantané récupéré dans le CT — pct push prend un fichier
#    à la fois, d'où la boucle.
scp -r 20260820-093240 root@<nouveau-noeud>:/tmp/
pct exec 200 -- mkdir -p /var/backups/postgresql/20260820-093240
for f in /tmp/20260820-093240/*; do
  pct push 200 "$f" "/var/backups/postgresql/20260820-093240/$(basename "$f")"
done
pct exec 200 -- chown -R postgres:postgres /var/backups/postgresql/20260820-093240
pct exec 200 -- chmod 700 /var/backups/postgresql/20260820-093240

# 5. Les rôles d'abord, les bases ensuite.
pct exec 200 -- sudo -u postgres psql -f /var/backups/postgresql/20260820-093240/globals.sql
pgbk restore forgejo 20260820-093240
pgbk verify  forgejo
```

**Si le nœud de remplacement porte un autre nom**, la copie hors-site s'y
range d'elle-même sous ce nom : `pg deploy` écrit un drop-in avec
`hostname -s` et le volume réel du CT. L'ancienne arborescence
`pve-eranikus/` reste intacte dans le bucket — rien ne l'écrase, rien ne la
supprime.

**À ne pas oublier une fois le service debout** :

- redéposer la clé du compte de service (`/root/.config/rclone/`) depuis
  OpenBao, puis rejouer `pg deploy` pour armer la copie hors-site ;
- remettre les mots de passe applicatifs — ou pas, voir
  [scénario 6](#6--les-secrets-sont-perdus) ;
- vérifier que les clients pointent la bonne IP (`pg_hba.conf` filtre en `/32`).

## 5 — Compromission du nœud

**L'historique distant est protégé par construction.** Le compte de service
porte `objectViewer` + `objectCreator` : il peut lire et créer, **il ne peut ni
écraser ni supprimer**. Un attaquant qui obtient la clé du nœud peut lire les
sauvegardes — ce qui est déjà grave — mais **ne peut pas détruire
l'historique**.

Dans l'ordre :

1. **Révoquer la clé du compte de service** dans la console GCP. Les objets
   déjà en place ne bougent pas.
2. **Considérer les empreintes SCRAM comme divulguées.** `globals.sql` part
   hors-site et contient les empreintes de tous les rôles. Changer les mots de
   passe de tous les locataires et du compte d'administration après la reprise
   (`pg deploy --admin`, et `ALTER ROLE <nom> PASSWORD '<nouveau>'` pour
   chaque locataire), puis les ranger dans OpenBao.
3. **Reconstruire sur du matériel sain**, en suivant le
   [scénario 4](#4--le-nœud-est-perdu). Ne pas réutiliser le nœud compromis.
4. **Choisir l'instantané avec soin** : prendre le plus récent *antérieur* à
   la compromission, pas le dernier. Le `MANIFEST` de chaque répertoire porte
   sa date.
5. Émettre une **nouvelle clé** de compte de service pour le nouveau nœud.

## 6 — Les secrets sont perdus

OpenBao perdu, ou mots de passe applicatifs introuvables, mais la base est
intacte.

**Il n'y a rien à récupérer, et c'est une bonne nouvelle.** Les mots de passe
ne sont stockés nulle part en clair — ni dans PostgreSQL, ni dans les dumps.
`globals.sql` contient les **empreintes SCRAM** : restaurer `globals.sql`
rend aux services leurs accès existants, sans que personne n'ait besoin de
connaître le mot de passe en clair. Les applications continuent de se
connecter avec ce qu'elles ont déjà dans leur configuration.

Ce qui est réellement perdu, c'est la capacité à **ouvrir une nouvelle
connexion à la main**. La porte de secours est la ligne `local all postgres
peer` de `pg_hba.conf` — **ne jamais la supprimer** :

```bash
pct enter 200
sudo -u postgres psql -c "ALTER ROLE jbwittner PASSWORD '<nouveau>';"
```

Puis ranger le nouveau mot de passe, et refaire de même pour chaque locataire
dont la configuration applicative aurait été perdue elle aussi.

## 7 — Un instantané est illisible

`pg_restore` échoue, le dump est tronqué, ou `rclone check` a signalé un objet
divergent.

**Prendre le précédent.** C'est la raison d'être des 14 jours de rétention
locale et des 365 jours distants :

```bash
pgbk list                                 # instantanés locaux, du plus récent
pgbk restore forgejo 20260819-234306      # celui d'avant
```

**Si c'est l'objet distant qui diverge** (`pgbk-offsite` sorti en code 3), il
ne peut pas être corrigé depuis le nœud — le compte de service n'a pas le
droit d'écraser. Suppression avec le compte personnel, puis renvoi :

```bash
gcloud auth login
gcloud storage rm gs://homelab-pgsql-backups-dc93212a/pve-eranikus/postgresql/<instantané>/<objet>
# sur le nœud
systemctl start pgbk-offsite.service
```

Détail : [runbook § 10](RUNBOOK.md#objet-distant-divergent--le-cas-à-traiter-à-la-main).

**Si plusieurs instantanés consécutifs sont illisibles**, le problème n'est pas
la restauration mais la sauvegarde : lire
`journalctl -u pg-backup --since '15 days ago'` dans le CT, et ne pas purger
ce qui reste tant que la cause n'est pas comprise.

## Après toute reprise

- [ ] `pgbk verify <base>` sur chaque base restaurée — ACL et propriétaires.
- [ ] Le service applicatif redémarre **et écrit**.
- [ ] `pgbk backup` : une sauvegarde fraîche de l'état reconstruit.
- [ ] `systemctl start pgbk-offsite.service` : la copie hors-site repart.
- [ ] `systemctl list-timers pg-backup.timer pgbk-offsite.timer` dans le CT et
      sur l'hôte : les deux automatismes sont réarmés.
- [ ] Les `pre-restore-*` sont supprimés une fois la reprise validée
      (`pgbk delete`), pas avant.
- [ ] Ce document est corrigé de ce qu'on a appris. **Une reprise réelle est
      le seul exercice qui ne ment pas** : ce qui a manqué doit y entrer
      pendant qu'on s'en souvient.
