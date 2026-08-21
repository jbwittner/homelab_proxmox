# PRA — CT Forgejo (`pve-eranikus`, CTID 400)

Une procédure de reprise **par scénario**, du dégât local à la perte du nœud.

Ce document se répète volontairement. En reprise on ne lit pas un document en
entier : on va à son cas, et on doit y trouver tout ce qu'il faut sans
naviguer. C'est le seul endroit du dépôt où la duplication est un choix.

> **Forgejo est la source de vérité d'ArgoCD.** Tant qu'elle est absente,
> aucune réconciliation GitOps n'est possible. Ce n'est pas une raison de se
> précipiter — c'en est une de suivre la procédure.

## Ce qu'on perd, et ce qu'on ne perd pas

| | |
|---|---|
| **RPO base** | **24 h** — la sauvegarde tourne à 02:45. Une panne à 02:44 perd la journée écoulée. |
| **RPO dépôts** | **celui du `vzdump` du CT** — à relever dans la planification du nœud. |
| **RPO hors-site** | **24 h de plus** — la copie part à 03:50. |
| **RTO** | **inconnu.** À mesurer par un [exercice](PRA-exercice.md). Une durée estimée de tête n'a aucune valeur le jour où on en a besoin. |

**La base et les dépôts sont sauvegardés séparément, et il faut les deux.** Une
base qui référence un dépôt absent du disque — ou l'inverse — donne une
instance qui démarre et se comporte n'importe comment. Le `MANIFEST` de chaque
instantané porte l'état de l'arborescence au moment du dump : c'est ce qui
permet d'apparier un `vzdump` à un dump.

## Ce que ce plan NE couvre PAS

Le dire explicitement, pour qu'une reprise réussie ne se confonde pas avec
« on est couvert » :

- **La perte de `secret_key`.** Elle n'est réparable par aucune restauration —
  voir [scénario 5](#5--les-secrets-sont-perdus). C'est le seul dégât
  irréversible de ce montage.
- **Un dépôt corrompu par un push accepté.** Le durcissement `fsckObjects`
  rejette les objets incohérents *en entrée* ; il ne répare rien de déjà écrit.
- **Une migration de schéma jouée par erreur** (passage en 16 ou 17). Elle est
  irréversible : le seul chemin est de restaurer la base d'avant.
- **Le contenu de GitHub.** Le miroir push est un chemin de reprise pour les
  objets git, pas une sauvegarde : ni tickets, ni demandes d'ajout, ni comptes,
  ni clés.

---

## Trouver son scénario

| Ce qu'on constate | Scénario |
|---|---|
| Un dépôt a disparu, une base est incohérente, un `DELETE` est parti trop loin | [1 — la base est perdue ou corrompue](#1--la-base-est-perdue-ou-corrompue) |
| `forgejo.service` ne démarre plus, redémarre en boucle | [2 — le service ne démarre plus](#2--le-service-ne-démarre-plus) |
| Les sessions sautent, les jetons ne marchent plus, les miroirs échouent | [2 — le service ne démarre plus](#2--le-service-ne-démarre-plus), section « secrets éphémères » |
| `pct list` ne montre plus le CT 400, ou il est irrécupérable | [3 — le conteneur est détruit](#3--le-conteneur-est-détruit) |
| `pve-eranikus` ne répond plus, disque mort, machine perdue | [4 — le nœud est perdu](#4--le-nœud-est-perdu) |
| `secret_key` est introuvable et le CT est à reconstruire | [5 — les secrets sont perdus](#5--les-secrets-sont-perdus) |
| Forgejo répond en local mais `forgejo.lan.wittner.tech` non | [6 — Traefik est absent](#6--traefik-est-absent) |
| ArgoCD n'arrive plus à tirer ses manifests | commencer par `fj status`, puis le scénario correspondant |

---

## 1 — La base est perdue ou corrompue

**Les dépôts sur disque ne sont PAS concernés.** On restaure la base seule, et
on la remet en face des dépôts tels qu'ils sont. Si les dépôts sont aussi
touchés, aller au [scénario 3](#3--le-conteneur-est-détruit).

### Constater

**Sur le nœud :**

```bash
fj status
fj list                    # quel instantané, de quand
```

Repérer l'instantané visé et **lire son manifeste** : il dit quel était l'état
des dépôts à ce moment-là.

```bash
pct exec 400 -- cat /var/backups/forgejo/<stamp>/MANIFEST
pct exec 400 -- ls -1 /var/lib/forgejo/repositories/*/ | wc -l   # aujourd'hui
```

Un `REPOS_COUNT` très différent du compte actuel veut dire que la base et les
dépôts ont divergé : la restauration rendra visibles des dépôts qui n'existent
plus, ou masquera des dépôts présents. Ce n'est pas bloquant, mais il faut le
savoir avant.

### Restaurer

**Sur le nœud.** Arrêter Forgejo d'abord : restaurer sous une application qui
écrit ne donne rien de cohérent.

```bash
pct exec 400 -- systemctl stop forgejo

pct exec 400 -- sudo -u postgres psql -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
   WHERE datname='forgejo' AND pid <> pg_backend_pid();"

pct exec 400 -- sudo -u postgres dropdb forgejo
pct exec 400 -- sudo -u postgres createdb forgejo -O forgejo -T template0 \
     --encoding UTF8 --lc-collate C --lc-ctype C

pct exec 400 -- sudo -u postgres pg_restore -d forgejo --no-owner --role=forgejo \
     /var/backups/forgejo/<stamp>/forgejo.dump
```

`--role=forgejo` est ce qui **rend les tables au locataire**. Sans lui, elles
appartiennent à `postgres` et Forgejo ne peut plus rien en faire.

### Réappliquer les ACL — obligatoire, pas optionnel

**Les ACL ne sont ni dans le dump ni dans un `globals.sql`.** Sans cette étape,
`PUBLIC` retrouve `CONNECT` et l'isolation disparaît **en silence** : la base
remonte, tout a l'air normal.

```bash
pct exec 400 -- sudo -u postgres psql -v ON_ERROR_STOP=1 \
     -f /etc/forgejo-git/init.sql

# Vérifier : la colonne « Access privileges » ne doit PAS être vide.
pct exec 400 -- sudo -u postgres psql -c '\l forgejo'
```

### Redémarrer et vérifier

```bash
pct exec 400 -- systemctl start forgejo
fj status
fj deploy --status          # le contrôle « ACL (après initialisation) » doit être OK
```

Puis, dans l'interface : un dépôt s'ouvre, son historique est là, une
connexion fonctionne.

---

## 2 — Le service ne démarre plus

### Regarder d'abord

**Sur le nœud :**

```bash
pct exec 400 -- systemctl status forgejo --no-pager
pct exec 400 -- journalctl -u forgejo -n 100 --no-pager
```

### Cas A — « Peer authentication failed for user "forgejo" »

La correspondance `git` → `forgejo` n'est pas chargée. Elle vit dans deux
fichiers qui travaillent ensemble, et le message n'en nomme aucun.

```bash
pct exec 400 -- readlink -f /etc/postgresql/*/main/pg_ident.conf
pct exec 400 -- readlink -f /etc/postgresql/*/main/pg_hba.conf
# les deux doivent pointer dans /etc/forgejo-git/

pct exec 400 -- systemctl reload postgresql
pct exec 400 -- systemctl restart forgejo
```

Si les liens sont faux ou absents : `fj deploy` les repose.

### Cas B — le conteneur ne voit pas son montage

```bash
pct exec 400 -- ls /etc/forgejo-git/
```

Vide ? Un `mpN` n'est relu **qu'au démarrage** :

```bash
pct reboot 400
```

### Cas C — secrets éphémères (sessions qui sautent, jetons cassés)

Symptôme : le service tourne, mais les connexions ne tiennent pas, les jetons
d'accès sont refusés, les miroirs échouent. Chercher dans le journal :

```bash
pct exec 400 -- journalctl -u forgejo --no-pager | grep -i 'app.ini'
```

Une ligne parlant d'écriture impossible sur `app.ini` veut dire que Forgejo
génère ses secrets en mémoire à chaque démarrage. Vérifier les quatre :

```bash
pct exec 400 -- ls -l /etc/forgejo/secrets/
# attendu : secret_key, internal_token, oauth2_jwt_secret, lfs_jwt_secret
# tous en -rw-r----- root:git
```

S'il en manque : les reposer depuis OpenBao
([runbook § 7](RUNBOOK.md#les-reposer-depuis-openbao)). **Ne pas les
régénérer** si l'instance a déjà servi — voir [scénario 5](#5--les-secrets-sont-perdus).

### Cas D — le binaire a disparu ou ne correspond plus

```bash
pct exec 400 -- /opt/forgejo/forgejo --version
fj version                 # ce qui devrait être là
fj deploy                  # retélécharge, vérifie, repose
```

### Cas E — PostgreSQL ne démarre pas

```bash
pct exec 400 -- systemctl status postgresql --no-pager
pct exec 400 -- pg_lsclusters
pct exec 400 -- tail -50 /var/log/postgresql/postgresql-*-main.log
```

Une erreur de syntaxe dans `10-forgejo.conf` ou `pg_hba.conf` empêche le
démarrage. Les fichiers étant des **symlinks vers le dépôt**, un `git pull`
malheureux suffit : revenir en arrière dans le dépôt et
`pct exec 400 -- systemctl restart postgresql`.

---

## 3 — Le conteneur est détruit

Le nœud va bien, le CT non. **Deux moitiés à récupérer**, et il faut les deux.

### Chemin le plus court : le vzdump

S'il existe un `vzdump` du CT 400, c'est le chemin le plus court — mais **il ne
contient PAS le volume `mp2`** (il porte `backup=0`) : il rend les dépôts et le
système, avec la base telle qu'elle était au moment du vzdump.

```bash
pct set 400 --protection 0        # la protection bloque la restauration
pvesm list <stockage-de-sauvegarde> | grep 400
pct restore 400 <volid> --force
pct start 400
```

Puis **remettre la protection**, et rejouer le déploiement pour retrouver ce
qui n'est pas dans le vzdump :

```bash
pct set 400 --protection 1
cd /root/homelab_proxmox && git pull
pve-eranikus/forgejo/fj deploy
fj status
```

Si la base du vzdump est plus ancienne que le dernier dump, restaurer la base
par-dessus — voir [scénario 1](#1--la-base-est-perdue-ou-corrompue). Comparer
d'abord `REPOS_LAST_MTIME` du manifeste au vzdump retenu.

### Sans vzdump : reconstruire

1. **Créer le conteneur** — [runbook § 1](RUNBOOK.md#1-création-du-conteneur).
2. **Reposer les secrets depuis OpenBao**, AVANT le premier démarrage —
   [runbook § 7](RUNBOOK.md#les-reposer-depuis-openbao). Si `secret_key` est
   perdu, aller au [scénario 5](#5--les-secrets-sont-perdus) avant d'aller plus
   loin.
3. **Déployer** :
   ```bash
   cd /root/homelab_proxmox && git pull
   pve-eranikus/forgejo/fj deploy
   ```
4. **Restaurer la base** — [scénario 1](#1--la-base-est-perdue-ou-corrompue).
5. **Restaurer les dépôts** depuis le vzdump le plus proche, ou depuis le
   miroir GitHub si les dépôts y sont poussés :
   ```bash
   # depuis le miroir, dépôt par dépôt, dans le CT :
   pct exec 400 -- sudo -u git git clone --mirror \
        https://github.com/<org>/<dépôt>.git \
        /var/lib/forgejo/repositories/<org>/<dépôt>.git
   ```
   Le miroir ne rend **que les objets git** : ni tickets, ni demandes d'ajout,
   ni comptes, ni clés SSH. C'est un chemin de reprise, pas une sauvegarde.
6. **Vérifier** : [runbook § 12](RUNBOOK.md#12-vérifications-de-recette).

---

## 4 — Le nœud est perdu

`pve-eranikus` ne répond plus. Deux constats, et ils tirent en sens opposés.

**Traefik survit** : il est sur `pve-ysera` (CT 201). Le routage tient donc
debout — il pointe simplement vers un dos mort. `forgejo.lan.wittner.tech`
répondra en 502 tant que le conteneur n'est pas remonté quelque part, et il
recommencera à servir dès qu'un CT reprendra l'IP `192.168.1.57`, sans
qu'aucune configuration Traefik ne soit à toucher. C'est le gain de ce
placement.

**On perd DEUX services d'un coup** : Forgejo et le cluster PostgreSQL
mutualisé du CT 200. Tous les locataires du CT 200 tombent avec. Cette
procédure ne traite que Forgejo ; l'autre est dans
[le PRA du CT 200](../../pgsql/doc/PRA.md#4--le-nœud-est-perdu), et l'ordre
dans lequel on les remonte est une décision à prendre sur le moment — la
source de vérité d'ArgoCD d'abord si le cluster Kubernetes est aussi à
réconcilier.

### Ce qu'on a ailleurs

| Où | Quoi |
|---|---|
| GCS | les dumps de la base, jusqu'à `<= 48 h` |
| Miroir GitHub | les objets git des dépôts qui y sont poussés |
| Ce dépôt | toute la configuration, l'épinglage, les unités |
| OpenBao | les quatre secrets |

**Les dépôts qui ne sont pas miroités et dont le vzdump est perdu avec le nœud
sont perdus.** C'est la raison pour laquelle le miroir sortant est dans les
« reste à faire » du README, et non un raffinement.

### Récupérer les dumps depuis GCS

Depuis n'importe quelle machine ayant la clé du compte de service :

```bash
rclone --config /root/.config/rclone/rclone.conf --gcs-bucket-policy-only \
  lsf gcs:homelab-pgsql-backups-dc93212a/pve-eranikus/forgejo/

rclone --config /root/.config/rclone/rclone.conf --gcs-bucket-policy-only \
  copy gcs:homelab-pgsql-backups-dc93212a/pve-eranikus/forgejo/<stamp>/ /tmp/<stamp>/
```

### Reconstruire ailleurs

1. Sur le nœud de repli, cloner ce dépôt.
2. Créer le CT — [runbook § 1](RUNBOOK.md#1-création-du-conteneur). **Reprendre
   l'IP `192.168.1.57`.** Ce n'est pas un confort : Traefik est resté debout
   sur `pve-ysera` et route déjà vers cette adresse. La reprendre remet le
   service en ligne sans toucher à une seule ligne de configuration Traefik ni
   à une entrée DNS. En changer transforme une reprise en chantier.
3. Reposer les secrets depuis OpenBao **avant le premier démarrage**.
4. `fj deploy --ctid 400` depuis le dépôt.
5. Pousser le dump récupéré dans le CT et le restaurer :
   ```bash
   pct push 400 /tmp/<stamp>/forgejo.dump /tmp/forgejo.dump
   ```
   puis suivre le [scénario 1](#1--la-base-est-perdue-ou-corrompue) à partir du
   `pg_restore`.
6. Restaurer les dépôts depuis les miroirs GitHub.
7. **Vérifier le routage — il n'y a rien à remonter.** Traefik est sur
   `pve-ysera`, il n'est pas tombé. Si l'IP a été reprise,
   `https://forgejo.lan.wittner.tech/` répond dès que le service démarre.
   Si elle n'a pas pu l'être, corriger l'adresse du backend dans
   [`pve-ysera/traefik/dynamic/forgejo.yaml`](../../../pve-ysera/traefik/dynamic/forgejo.yaml)
   — deux lignes, `http://…:3000` pour le web et `…:2222` pour le routeur TCP
   SSH — puis commiter. Traefik surveille son répertoire dynamique et reprend
   sans redémarrage.
8. **Ne pas oublier le CT 200.** Il est tombé avec le nœud, et tous ses
   locataires avec lui : [PRA du CT 200](../../pgsql/doc/PRA.md).

---

## 5 — Les secrets sont perdus

**C'est le seul dégât irréversible de ce montage.** Aucune restauration ne le
répare, parce qu'il ne s'agit pas de données perdues mais de données devenues
illisibles.

`secret_key` chiffre, **dans la base**, les jetons d'accès, les secrets 2FA et
les mots de passe des miroirs. Sans lui, une base restaurée remonte
parfaitement — et tout ce qu'elle contient de chiffré est perdu.

### D'abord : chercher vraiment

Avant de conclure, épuiser les endroits où il peut être :

```bash
bao kv get homelab/forgejo                       # OpenBao
pct exec 400 -- ls -l /etc/forgejo/secrets/      # le CT, s'il existe encore
```

Et dans un `vzdump` du CT, même ancien : les secrets sont dans `/etc/forgejo/`,
qui **est** dans le vzdump. Monter l'archive et les en extraire est le premier
réflexe utile.

### Si le secret est réellement perdu

Ce qui est récupérable, et ce qui ne l'est pas :

| Perdu définitivement | Récupérable |
|---|---|
| Jetons d'accès API des utilisateurs | Les dépôts (objets git) |
| Secrets 2FA — **tous les comptes 2FA sont à réinitialiser** | Les tickets, demandes d'ajout, commentaires |
| Mots de passe des miroirs push | Les comptes et leurs mots de passe (hachés, indépendants de `secret_key`) |
| Jetons OAuth2 émis | Les clés SSH publiques |

Procédure :

1. Générer de nouveaux secrets : `fj deploy --secrets` sur l'instance
   reconstruite.
2. **Les ranger dans OpenBao immédiatement.**
3. Prévenir les utilisateurs : jetons API à recréer, 2FA à réinscrire.
4. Recréer les miroirs push avec de nouveaux jetons GitHub
   ([runbook § 11](RUNBOOK.md#11-miroir-sortant-vers-github)).

---

## 6 — Traefik est absent

Forgejo tourne, mais `forgejo.lan.wittner.tech` ne répond plus : c'est le CT 201
qui manque, pas le 400.

**La source de vérité reste utilisable en direct.** C'est exactement ce que
l'autonomie du CT 400 est censée donner :

```bash
# HTTP, en visant le conteneur
git clone http://192.168.1.57:3000/<org>/<dépôt>.git

# SSH, en visant le serveur interne de Forgejo
git clone ssh://git@192.168.1.57:2222/<org>/<dépôt>.git
```

L'interface web répond aussi sur `http://192.168.1.57:3000/`, en clair et avec
des redirections qui pointeront vers `ROOT_URL` — c'est inconfortable, pas
bloquant.

Pour ArgoCD, le temps de remonter Traefik, la source du dépôt peut être
basculée sur `http://192.168.1.57:3000/…`. **Le rebasculer ensuite** : laisser
une URL en IP dans les manifests transforme un dépannage en dette.

Remonter Traefik : `pve-ysera/traefik/` de ce dépôt.
