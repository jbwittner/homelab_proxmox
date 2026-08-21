# PRA — CT Forgejo (`pve-eranikus`, CTID 400)

Une procédure de reprise **par scénario**, du dégât local à la perte du nœud.

Ce document se répète volontairement. En reprise on ne lit pas un document en
entier : on va à son cas, et on doit y trouver tout ce qu'il faut sans
naviguer. C'est le seul endroit du dépôt où la duplication est un choix.

> **Forgejo est la source de vérité d'ArgoCD.** Tant qu'elle est absente,
> aucune réconciliation GitOps n'est possible. Ce n'est pas une raison de se
> précipiter — c'en est une de suivre la procédure.

## La chose à comprendre avant tout le reste

**Forgejo est en deux morceaux, sur deux conteneurs.**

```
CT 400   le service, les DÉPÔTS, les secrets, le binaire
CT 200   la BASE — un locataire du cluster mutualisé
```

Une reprise complète demande **les deux**, et ils ne sont pas sauvegardés par
le même mécanisme ni au même instant. C'est le prix de la mutualisation, et
c'est la seule difficulté réelle de ce plan.

## Ce qu'on perd, et ce qu'on ne perd pas

| | |
|---|---|
| **RPO base** | **24 h** — `pg-backup` tourne à 02:30 sur le CT 200. |
| **RPO dépôts** | **celui du `vzdump` du CT 400** — à relever dans la planification du nœud. |
| **RPO hors-site** | **24 h de plus** pour la base — `pgbk-offsite` part à 03:30. |
| **RTO** | **inconnu.** À mesurer par un [exercice](PRA-exercice.md). Une durée estimée de tête n'a aucune valeur le jour où on en a besoin. |

**Retenir le dump le plus proche du vzdump, et de préférence POSTÉRIEUR.** Une
base plus récente que les dépôts référence au pire quelques dépôts absents : ça
se voit, et ça se corrige. Une base plus ancienne ignore des dépôts présents
sur le disque — ils sont simplement invisibles dans l'interface, et on les
croit perdus.

## Ce que ce plan NE couvre PAS

Le dire explicitement, pour qu'une reprise réussie ne se confonde pas avec
« on est couvert » :

- **La perte de `secret_key` sans vzdump.** Elle n'est réparable par aucune
  restauration de base — voir [scénario 5](#5--les-secrets-sont-perdus).
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
| Un dépôt a disparu de l'interface, la base est incohérente, un `DELETE` est parti trop loin | [1 — la base est perdue ou corrompue](#1--la-base-est-perdue-ou-corrompue) |
| `forgejo.service` ne démarre plus, ou redémarre en boucle | [2 — le service ne démarre plus](#2--le-service-ne-démarre-plus) |
| Les sessions sautent, les jetons ne marchent plus, les miroirs échouent | [2 — le service ne démarre plus](#cas-c--secrets-éphémères), cas C |
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

**Tout se passe sur le CT 200**, pas sur le CT 400. Forgejo n'a aucun outil de
restauration : sa base appartient au cluster mutualisé.

### Constater

**Sur le nœud :**

```bash
fj status                  # le maillon « base (CT 200) » — répond-elle ?
pg status                  # les trois maillons de la sauvegarde
pg list                    # quel instantané, de quand
```

### Arrêter Forgejo AVANT de restaurer

Restaurer sous une application qui écrit ne donne rien de cohérent.

```bash
pct exec 400 -- systemctl stop forgejo
```

### Restaurer

```bash
pg restore forgejo                 # depuis le dernier instantané
pg restore forgejo 20260821        # ou depuis le plus récent de ce jour-là
```

La question de confirmation est posée **sur le nœud**, et elle porte sur
l'instantané **réellement visé** — `20260821` désigne le plus récent de ce
jour-là, pas ce qui a été tapé.

`pg restore` réapplique les ACL lui-même : c'est dans sa procédure, parce que
**les ACL ne sont ni dans le dump ni dans `globals.sql`**. Le vérifier quand
même coûte une commande :

```bash
pg verify forgejo
```

### Redémarrer et vérifier

```bash
pct exec 400 -- systemctl start forgejo
fj status
```

Puis, dans l'interface : un dépôt s'ouvre, son historique est là, une connexion
fonctionne. **Si des dépôts manquent à l'appel**, c'est l'appariement : la base
restaurée est plus ancienne que les dépôts sur disque. Les dépôts ne sont pas
perdus — ils sont invisibles. Reprendre avec un instantané plus récent.

---

## 2 — Le service ne démarre plus

### Regarder d'abord

**Sur le nœud :**

```bash
pct exec 400 -- systemctl status forgejo --no-pager
pct exec 400 -- journalctl -u forgejo -n 100 --no-pager
fj status
```

### Cas A — la base refuse la connexion

Trois messages, trois causes, et aucun ne nomme sa cause. `fj status` rend la
première ligne du refus telle quelle, parce que c'est elle qui tranche :

| Message | Cause | Remède |
|---|---|---|
| `no pg_hba.conf entry for host "192.168.1.57"` | la ligne du locataire manque dans le `pg_hba.conf` du CT 200, ou elle est **après** le `reject` | l'ajouter avant le `reject`, puis `pg deploy` |
| `password authentication failed for user "forgejo"` | le mot de passe déposé n'est pas celui du rôle | le reprendre dans OpenBao, ou `ALTER ROLE` depuis la porte `peer` du CT 200 |
| `database "forgejo" does not exist` | le locataire n'a jamais été créé | `pg deploy --tenant forgejo` sur le CT 200 |

La ligne attendue, sur le CT 200 :

```
hostssl   forgejo     forgejo       192.168.1.57/32         scram-sha-256
```

### Cas B — le CT 200 ne répond pas

```bash
pct status 200
pct exec 200 -- systemctl status postgresql --no-pager
```

Forgejo sait attendre sa base et réessaie. Remonter le CT 200 suffit — voir
[le PRA du CT 200](../../pgsql/doc/PRA.md).

### Cas C — secrets éphémères

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
# plus db_password, qui n'est pas de la même nature
# tous en -rw-r----- root:git
```

S'il en manque : les reposer depuis OpenBao
([runbook § 7](RUNBOOK.md#les-reposer-depuis-openbao)). **Ne pas les
régénérer** si l'instance a déjà servi — voir
[scénario 5](#5--les-secrets-sont-perdus).

### Cas D — le conteneur ne voit pas son montage

```bash
pct exec 400 -- ls /etc/forgejo-git/
```

Vide ? Un `mpN` n'est relu **qu'au démarrage** :

```bash
pct reboot 400
```

### Cas E — le binaire a disparu ou ne correspond plus

```bash
pct exec 400 -- /opt/forgejo/forgejo --version
fj version                 # ce qui devrait être là
fj deploy                  # retélécharge, vérifie, repose
```

---

## 3 — Le conteneur est détruit

Le nœud va bien, le CT 400 non. **La base n'est pas concernée** : elle est dans
le CT 200, intacte. C'est le principal bénéfice de la mutualisation en reprise
— il n'y a qu'une moitié à reconstruire.

### Chemin le plus court : le vzdump

```bash
pct set 400 --protection 0        # la protection bloque la restauration
pvesm list <stockage-de-sauvegarde> | grep 400
pct restore 400 <volid> --force
pct start 400
```

Le vzdump contient les dépôts, **les secrets** (`/etc/forgejo`) et le binaire.
Il ne contient pas la base — elle n'a pas bougé.

Puis remettre la protection et rejouer le déploiement :

```bash
pct set 400 --protection 1
cd /root/homelab_proxmox && git pull
pve-eranikus/forgejo/fj deploy
fj status
```

**Vérifier l'appariement** : la base du CT 200 est *actuelle*, les dépôts
viennent du vzdump donc plus anciens. Des dépôts créés après le vzdump seront
référencés par la base sans exister sur disque. Ils apparaîtront cassés dans
l'interface — c'est visible, et c'est le bon sens de l'écart.

### Sans vzdump : reconstruire

1. **Créer le conteneur** — [runbook § 1](RUNBOOK.md#1-création-du-conteneur).
2. **Reposer les secrets depuis OpenBao**, AVANT le premier démarrage —
   [runbook § 7](RUNBOOK.md#les-reposer-depuis-openbao). Si `secret_key` est
   perdu, aller au [scénario 5](#5--les-secrets-sont-perdus) d'abord.
3. **Déposer le mot de passe de la base** — il est dans OpenBao aussi
   ([runbook § 3](RUNBOOK.md#créer-le-locataire--sur-le-ct-200-pas-ici)).
4. **Déployer** :
   ```bash
   cd /root/homelab_proxmox && git pull
   pve-eranikus/forgejo/fj deploy
   ```
5. **Restaurer les dépôts** depuis le miroir GitHub, s'il existe :
   ```bash
   pct exec 400 -- sudo -u git git clone --mirror \
        https://github.com/<org>/<dépôt>.git \
        /var/lib/forgejo/repositories/<org>/<dépôt>.git
   ```
   Le miroir ne rend **que les objets git** : ni tickets, ni demandes d'ajout,
   ni comptes, ni clés SSH. Ceux-là sont dans la base, qui n'a pas bougé.
6. **Vérifier** : [runbook § 12](RUNBOOK.md#12-vérifications-de-recette).

---

## 4 — Le nœud est perdu

`pve-eranikus` ne répond plus. **On perd les DEUX conteneurs** : Forgejo et le
cluster PostgreSQL mutualisé — donc tous ses locataires, pas seulement celui-ci.

**Traefik survit** : il est sur `pve-ysera` (CT 201). Le routage tient debout et
pointe vers un dos mort ; il recommencera à servir dès qu'un conteneur reprendra
l'IP `192.168.1.57`, sans qu'aucune configuration Traefik ne soit à toucher.

Cette procédure ne traite que Forgejo. Le cluster est dans
[le PRA du CT 200](../../pgsql/doc/PRA.md#4--le-nœud-est-perdu), et **il passe
en premier** : sans base, Forgejo n'a rien à quoi se connecter.

### Ce qu'on a ailleurs

| Où | Quoi |
|---|---|
| GCS | les dumps de la base, jusqu'à `<= 48 h` |
| Miroir GitHub | les objets git des dépôts qui y sont poussés |
| Ce dépôt | toute la configuration, l'épinglage, la clé de signature |
| OpenBao | les quatre secrets **et** le mot de passe de la base |

**Les dépôts qui ne sont pas miroités et dont le vzdump est perdu avec le nœud
sont perdus.** C'est la raison pour laquelle le miroir sortant est dans les
« reste à faire » du README, et non un raffinement.

### L'ordre

1. **Remonter le CT 200 d'abord**, avec sa base — voir son PRA. C'est lui qui
   porte le locataire `forgejo`.
2. Sur le nœud de repli, cloner ce dépôt.
3. Créer le CT 400 — [runbook § 1](RUNBOOK.md#1-création-du-conteneur).
   **Reprendre l'IP `192.168.1.57`** : Traefik route déjà vers elle, et le
   `pg_hba.conf` du CT 200 l'autorise en `/32`. En changer demande de toucher
   aux deux.
4. Reposer les secrets et le mot de passe depuis OpenBao, **avant le premier
   démarrage**.
5. `fj deploy --ctid 400` depuis le dépôt.
6. Restaurer les dépôts depuis les miroirs GitHub.
7. **Vérifier le routage — il n'y a rien à remonter.** Si l'IP a été reprise,
   `https://forgejo.lan.wittner.tech/` répond dès que le service démarre. Sinon,
   corriger l'adresse du backend dans
   [`pve-ysera/traefik/dynamic/forgejo.yaml`](../../../pve-ysera/traefik/dynamic/forgejo.yaml)
   — deux lignes — puis commiter. Traefik surveille son répertoire dynamique.

---

## 5 — Les secrets sont perdus

**C'est le seul dégât irréversible de ce montage.** Aucune restauration de base
ne le répare, parce qu'il ne s'agit pas de données perdues mais de données
devenues illisibles.

`secret_key` chiffre, **dans la base**, les jetons d'accès, les secrets 2FA et
les mots de passe des miroirs. Sans lui, une base restaurée remonte
parfaitement — et tout ce qu'elle contient de chiffré est perdu.

### D'abord : chercher vraiment

Trois endroits, et le troisième est souvent oublié :

```bash
bao kv get homelab/forgejo                       # OpenBao
pct exec 400 -- ls -l /etc/forgejo/secrets/      # le CT, s'il existe encore
```

Et **dans un `vzdump` du CT 400, même ancien** : les secrets sont dans
`/etc/forgejo/`, qui **est** dans le vzdump. Monter l'archive et les en
extraire est le premier réflexe utile — et c'est une raison de plus pour que le
stockage de sauvegarde ne soit pas plus lisible que le conteneur lui-même.

### Si le secret est réellement perdu

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

> Le mot de passe de la base, lui, n'est **pas** concerné : il n'est pas
> chiffré par `secret_key`, il est dans OpenBao et dans `pg_hba` côté serveur.
> Le perdre se répare par un `ALTER ROLE` depuis la porte `peer` du CT 200.

---

## 6 — Traefik est absent

Forgejo tourne, mais `forgejo.lan.wittner.tech` ne répond plus : c'est le CT 201
qui manque, sur `pve-ysera`, pas le 400.

**La source de vérité reste utilisable en direct** :

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
