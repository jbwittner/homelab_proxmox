# Runbook — CT Forgejo (`pve-eranikus`, CTID 400)

Le détail : création, conception, pièges. Ce qu'on tape au quotidien est dans
le [README](../README.md).

**Les numéros de section sont stables.** Des messages d'erreur du code et le
`Documentation=` des unités systemd y renvoient par numéro. Déplacer une
section, c'est aussi corriger ces renvois-là.

## Sommaire

- [0. Retrait de l'instance 16.0 existante](#0-retrait-de-linstance-160-existante)
- [1. Création du conteneur](#1-création-du-conteneur)
- [2. Déploiement depuis l'hôte : `fj deploy`](#2-déploiement-depuis-lhôte--fj-deploy)
- [3. PostgreSQL co-localisé](#3-postgresql-co-localisé)
- [4. La version épinglée](#4-la-version-épinglée)
- [5. Arborescence et configuration](#5-arborescence-et-configuration)
- [6. Routage Traefik](#6-routage-traefik)
- [7. Les secrets](#7-les-secrets)
- [8. Durcissement git](#8-durcissement-git)
- [9. Sauvegardes](#9-sauvegardes)
- [10. Copie hors-site vers GCS](#10-copie-hors-site-vers-gcs)
- [11. Miroir sortant vers GitHub](#11-miroir-sortant-vers-github)
- [12. Vérifications de recette](#12-vérifications-de-recette)

---

## 0. Retrait de l'instance 16.0 existante

Un CT Forgejo en **16.0** a été posé par le script communautaire. Il est réputé
vide. Le retirer est un geste **destructif** : on ne le joue pas sans avoir
constaté ce vide soi-même.

> **La 16.0 n'est pas LTS** : fin de support le 29 octobre 2026. C'est la
> raison du remplacement, et non un caprice de numérotation.

### Constater qu'il est vide — les deux vérifications, pas une

Un CT est vide quand **ni les dépôts sur disque ni les comptes en base** ne
contiennent quoi que ce soit. Vérifier l'un sans l'autre laisse passer le cas
où quelqu'un a créé un compte sans encore pousser de dépôt.

```bash
CT=<ctid de l'ancienne instance>          # à relever dans « pct list »

# 1. Les dépôts sur disque. Attendu : aucune ligne.
pct exec $CT -- find /var/lib/gitea/repositories /var/lib/forgejo/repositories \
     -maxdepth 2 -name '*.git' 2>/dev/null

# 2. Les comptes en base. Attendu : 0.
pct exec $CT -- sudo -u postgres psql -tAc \
     "SELECT count(*) FROM \"user\"" -d forgejo 2>/dev/null

# 3. Les objets LFS et les pièces jointes. Attendu : aucune ligne.
pct exec $CT -- find /var/lib/forgejo/data/lfs /var/lib/forgejo/data/attachments \
     -type f 2>/dev/null
```

**Si l'une de ces trois commandes renvoie autre chose que du vide : s'arrêter.**
Il y a des données. Les sauvegarder et décider ensuite — la migration 16 → 15
n'est **pas** possible par restauration de dump : le schéma de la 16 a déjà
migré, et PostgreSQL n'a pas de « démigration ». Le seul chemin serait
`forgejo dump` puis import manuel, dépôt par dépôt.

### Retirer

Une fois le vide constaté, et **seulement** ensuite :

```bash
pct stop $CT
pct set $CT --protection 0        # la protection bloque la destruction
pct destroy $CT --purge           # --purge retire aussi les entrées de sauvegarde
```

Vérifier ensuite que le CTID n'apparaît plus dans `pct list`, et que Traefik ne
route plus vers son IP.

---

## 1. Création du conteneur

`fj deploy` ne crée **pas** le conteneur, délibérément : la création est un
geste unique, et un déployeur rejouable n'a pas à porter du code qui ne servira
qu'une fois. Elle se tape ici, sur le nœud.

**Relever d'abord le nom exact du modèle** — il porte un numéro de révision qui
change à chaque publication, et un nom deviné échoue avec un message qui parle
de stockage plutôt que de modèle :

```bash
pveam list local | grep debian-13
# à défaut : pveam update && pveam available | grep debian-13
```

```bash
pct create 400 local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst \
    --hostname forgejo \
    --unprivileged 1 \
    --features nesting=1 \
    --cores 2 \
    --memory 2048 \
    --swap 512 \
    --rootfs local-lvm:32 \
    --net0 name=eth0,bridge=vmbr0,ip=192.168.1.57/24,gw=192.168.1.254 \
    --nameserver 192.168.1.2 \
    --onboot 1 \
    --startup order=1 \
    --description 'Forgejo — source de vérité ArgoCD. Version ÉPINGLÉE 15.0 LTS.
NE JAMAIS mettre à jour par script communautaire. Voir
pve-eranikus/forgejo/doc/RUNBOOK.md section 4.'

pct start 400
```

Puis, dans le conteneur, ce que `fj deploy` ne peut pas faire à sa place
— il lui faut un `python3` et un `sudo` pour entrer :

```bash
pct exec 400 -- apt-get update
pct exec 400 -- apt-get install -y python3-minimal sudo
```

Le reste — paquets, utilisateur, arborescence, base, binaire, unités — est le
travail de `fj deploy` ([§ 2](#2-déploiement-depuis-lhôte--fj-deploy)).

### Pourquoi ces valeurs

| Choix | Raison |
|---|---|
| **32 Go** de disque | Les 10 Go du script communautaire sont trop justes dès qu'il y a du LFS ou un miroir. Agrandir plus tard demande un `pct resize` et une extension de système de fichiers ; le faire maintenant coûte zéro. |
| **non privilégié** | Forgejo exécute des hooks git écrits par les utilisateurs des dépôts. |
| **`nesting=1`** | Obligatoire sur Debian 13 — voir ci-dessous. |
| **IP statique** | Une source de vérité ne dépend pas du DHCP. Un bail perdu, et Traefik route vers le vide. |
| **`onboot=1` + `startup order=1`** | Elle doit être debout **avant** que le reste ne cherche à se réconcilier. `onboot` dit *s'il* démarre, `startup` dit *dans quel ordre* : les deux sont indépendants, et un CT avec un ordre mais sans `onboot` ne démarre jamais. |
| **2 Go / 2 vCPU** | Une instance à quelques utilisateurs, PostgreSQL compris. Les valeurs de `ct/10-forgejo.conf` sont dimensionnées pour cette RAM : la changer demande d'y toucher aussi. |

### Le piège du nesting

Sans `nesting=1`, les unités qui montent un tmpfs pour les *credentials*
systemd — ce que font `forgejo.service` et `fj-backup.service` avec
`PrivateTmp=true` — échouent en :

```
Failed to set up credentials: Permission denied
... status=243/CREDENTIALS
```

Le conteneur démarre quand même, en état dégradé, **sans que rien ne le
signale**. `fj deploy` pose la feature s'il la trouve absente, en préservant
les autres, et déclare qu'un redémarrage est nécessaire — un `mpN` comme une
feature n'est relu qu'au démarrage.

---

## 2. Déploiement depuis l'hôte : `fj deploy`

Un parcours, trois modes, un bilan. L'ordre des étapes est une **donnée**,
lisible dans [`fjtool/plan.py`](../fjtool/plan.py), et non une suite d'appels.

```
A  prérequis du conteneur      protection, nesting, onboot, mp1, mp2, startup
   ── barrière : le CT redémarre ici, et nulle part ailleurs
D  outillage du nœud           fj, arbre d'import, CTID consigné, gnupg, rclone
B  pose dans le conteneur      paquets, utilisateur git, arborescence,
                               configuration PostgreSQL, app.ini, unités, moteur
   ── barrière : systemd relit ses unités
V  installation binaire        version épinglée, clé, téléchargement vérifié
P  la base                     base + rôle, ACL, connexion peer éprouvée
G  les secrets                 AVANT le premier démarrage
   ── barrière
B  le service                  forgejo, fj-backup.timer
G  première sauvegarde
F  copie hors-site             clé GCP, rclone.conf, drop-in, unités, armement
H  ce qui ne doit pas être là  orphelins, automatismes de mise à jour
C  contrôles                   en dernier, sinon ils répondent sur l'état d'avant
```

### Les ordres qui comptent

**Les secrets avant le premier démarrage.** C'est l'ordre le plus important du
plan. Démarrer d'abord laisserait Forgejo générer ses secrets lui-même et
tenter de réécrire un `app.ini` qui vient d'un montage en **lecture seule** —
voir [§ 7](#7-les-secrets).

**L'outillage du nœud avant la section V.** C'est le nœud qui télécharge et qui
vérifie ; sans `gnupg`, il n'y a rien à vérifier.

**La base avant le service.** Sinon le premier démarrage n'est qu'une suite
d'échecs de connexion, que quelqu'un lira comme une panne.

**La première sauvegarde avant le hors-site.** Sinon la première copie n'a rien
à transférer et sort en « environnement inutilisable ».

**Les contrôles en dernier.** Un contrôle joué au milieu répond sur l'état
d'*avant* les poses qui le suivent.

### Ce qu'il ne fait pas

| Geste | Pourquoi il reste manuel |
|---|---|
| Créer le conteneur | Geste unique — [§ 1](#1-création-du-conteneur) |
| Déposer la clé GCP | C'est un secret — [§ 10](#10-copie-hors-site-vers-gcs) |
| Déposer `ct/RELEASE-KEY.asc` | Un ancrage de confiance s'obtient hors du canal qu'il valide — [§ 4](#la-clé-de-publication) |
| Renseigner l'IP de Traefik | Elle dépend de la machine ; le déploiement refuse un bilan vert tant que le marqueur `@@TRAEFIK_IP@@` est là |
| Résoudre `ct/VERSION` | C'est une décision — [§ 4](#4-la-version-épinglée) |

---

## 3. PostgreSQL co-localisé

### Pourquoi dans ce conteneur, et pas sur le CT 200

Le cluster mutualisé du CT 200 est sur le **même nœud**. L'argument n'est donc
pas la disponibilité — une panne de `pve-eranikus` emporte les deux de toute
façon, et prétendre le contraire serait se raconter une histoire. Il est
ailleurs, et il tient en trois points.

**Les cycles de vie sont incompatibles.** Le CT 200 relève du tier 200–299 :
il est posé et mis à jour par un script communautaire. Le CT 400 relève du
tier 400–499 : sa version est gelée et ne bouge que sur décision. Faire
dépendre un service dont on gèle la version d'un cluster qui, lui, se met à
jour tout seul réintroduit par la bande exactement ce que l'épinglage
empêche.

**Le rayon de panne.** Le CT 200 sert plusieurs locataires, donc ses fenêtres
de maintenance sont celles du plus bruyant. Un `pg deploy` malheureux, un
redémarrage du cluster, la restauration d'un autre locataire : chacun arrête
la source de vérité d'ArgoCD — au moment précis où c'est elle qui doit
permettre de réparer le reste.

**La reprise tient dans un seul conteneur.** Restaurer Forgejo, c'est
restaurer un CT : la base et les dépôts reviennent ensemble, depuis un dump et
un vzdump du même objet. Avec la base ailleurs, il faudrait apparier deux
conteneurs et deux jeux de sauvegardes — dont un mutualisé, qu'on ne peut pas
restaurer sans toucher aux autres locataires.

Conséquence assumée : un second cluster PostgreSQL à faire vivre, à sauvegarder
et à mettre à jour, sur la même machine que le premier. C'est le prix, et il
est payé sciemment.

### Socket Unix, peer, et aucune écoute TCP

```
listen_addresses = ''        # ct/10-forgejo.conf
HOST = /var/run/postgresql   # ct/app.ini, [database]
```

PostgreSQL n'ouvre **aucun** socket TCP — ni sur le LAN, ni sur la boucle
locale. Le seul chemin d'entrée est la socket Unix, que seuls les processus de
ce conteneur peuvent atteindre. Trois conséquences :

1. **Aucun mot de passe de base à faire vivre**, à faire tourner, ni à perdre.
2. **Le piège documenté pour le CT 200 ne peut pas se produire.** Là-bas,
   `listen_addresses` doit valoir `'*'` et non une IP explicite : sinon
   PostgreSQL peut démarrer avant que `eth0` ne porte son adresse, n'ouvrir que
   la boucle locale, et se déclarer `active (running)` malgré tout. Ici on
   n'écoute nulle part, donc il n'y a pas de course.
3. Le contrôle `fj deploy` lit `ss -lntp` et **lève une alarme si une socket
   TCP apparaît**. C'est l'inverse du contrôle du CT 200, et c'est voulu.

### Le piège de `pg_ident`

Forgejo tourne sous l'utilisateur système **`git`** et se connecte au rôle SQL
**`forgejo`**. En authentification `peer`, PostgreSQL exige que les deux noms
soient identiques — sauf si une correspondance le dit. Sans elle :

```
FATAL:  Peer authentication failed for user "forgejo"
```

C'est l'échec le plus probable d'un premier démarrage, et le message ne nomme
**ni** `pg_hba.conf` **ni** `pg_ident.conf`. Les deux fichiers travaillent
ensemble :

```
# ct/pg_hba.conf
local     forgejo     forgejo     peer  map=forgejo

# ct/pg_ident.conf
# MAPNAME   SYSTEM-USERNAME   PG-USERNAME
forgejo     git               forgejo
```

`fj deploy` **éprouve** cette connexion pour de bon — il se connecte sous
`git`, comme le service le fera — plutôt que de se contenter de lire les
fichiers. Un fichier juste et non rechargé donnerait un bilan vert et un
service en échec.

### Rejouer l'initialisation à la main

`ct/init.sql` est **idempotent** : chaque ordre est gardé, rien n'est détruit.

```bash
pct exec 400 -- sudo -u postgres psql -v ON_ERROR_STOP=1 \
     -f /etc/forgejo-git/init.sql
```

C'est aussi le remède quand les ACL ont disparu — voir
[§ 9](#les-acl-ne-sont-pas-dans-le-dump).

---

## 4. La version épinglée

### Pourquoi pas `community-scripts/ct/forgejo.sh`

Son appel est :

```bash
fetch_and_deploy_codeberg_release "forgejo" "forgejo" "singlefile" "latest" ...
```

`"latest"` est **en dur**. Le script ne peut donc installer que la dernière
publication — aujourd'hui la 16.0, dont le support s'arrête le 29 octobre 2026.
Pire, sa fonction `update_script()` redéploie `latest` **sans prompt et sans
sauvegarde préalable** : jouée en octobre, elle fait sauter une majeure, avec
une migration de schéma que rien ne rejoue à l'envers.

C'est la raison d'être de tout ce répertoire.

### Comment la version est tenue

| Élément | Rôle |
|---|---|
| `ct/VERSION` | une ligne « v15.0.x », le reste est commentaire |
| `fj version` | la lit et la valide |
| `fj version --resolve` | interroge Codeberg, retient la dernière 15.0.x stable, réécrit le fichier — **et n'installe rien** |
| `fj deploy` | pose exactement ce que le fichier dit, et rien d'autre |

**Résoudre et poser sont deux commandes**, et c'est toute la différence. Un
déploiement ne doit jamais dépendre de ce qu'un serveur distant répond ce
jour-là.

La résolution écarte, dans cet ordre : les brouillons (leurs artefacts peuvent
changer sous le même tag), les pré-versions, et tout ce qui n'est pas sur la
branche `15.0`. Le tri est **numérique** : `v15.0.10` est plus récent que
`v15.0.9`, ce qu'un tri de chaînes conclurait à l'envers.

Une version collée à la main hors de la branche LTS est **refusée** :

```
« v16.0.0 » n'est pas sur la branche LTS 15.0 — changer de branche est une
décision, pas un correctif : voir doc/RUNBOOK.md section 4
```

### La clé de publication

`fj deploy` vérifie **la somme de contrôle ET la signature**. La somme seule ne
prouve rien : elle voyage sur le même canal que le binaire, donc qui peut
remplacer l'un peut remplacer l'autre. C'est la signature qui rattache
l'artefact à une clé — et **cette clé doit avoir été obtenue autrement que par
le canal qu'elle sert à valider**.

D'où la question : d'où vient la clé, et comment sait-on que c'est la bonne ?

**Ce qu'on fait ici est de la confiance à la première utilisation, puis de
l'épinglage.** La clé est récupérée UNE fois, son empreinte est écrite dans le
dépôt et commitée. À partir de là, chaque déploiement vérifie que la clé qui
sert à valider le binaire est toujours celle-là.

```bash
fj key                  # l'empreinte épinglée, et si la clé du dépôt correspond
fj key --fetch          # récupère la clé, écrit ct/RELEASE-KEY.asc,
                        # et épingle son empreinte — N'INSTALLE RIEN
fj key --fetch --from https://exemple/cle.asc   # autre URL
fj key --fetch --from ./cle-recuperee.asc       # ou un fichier local
```

Deux fichiers en sortent, et ce n'est pas une redondance :

| Fichier | Rôle |
|---|---|
| `ct/RELEASE-KEY.asc` | le bloc de clé — un pavé dont le `git diff` ne dit rien à un humain |
| `ct/RELEASE-KEY.fingerprint` | l'empreinte, sur une ligne — **c'est elle qui rend un changement de clé visible en revue** |

### D'où vient la clé, et pourquoi de là

Du **Web Key Directory** de `contact@forgejo.org`, que la page de
téléchargement officielle désigne :

```
https://openpgpkey.forgejo.org/.well-known/openpgpkey/forgejo.org/hu/dj3498u4hyyarh35rkjfnghbjxug6b19
```

Ce n'est pas un détail d'implémentation. **Ce WKD vit sur un domaine différent
de celui d'où vient le binaire** (`openpgpkey.forgejo.org` contre
`codeberg.org`) : la clé et l'artefact ne voyagent donc pas par le même canal,
ce qui est précisément la propriété qui fait qu'une vérification de signature
vaut mieux qu'une somme de contrôle.

L'adresse est **dérivée** de l'adresse de courriel — le `hu/…` est un hachage
de sa partie locale — donc elle est stable tant que l'adresse l'est. `--from`
reste disponible pour n'importe quelle autre source, URL ou fichier local.

### La confrontation à deux canaux, faite le 21 août 2026

Elle est reproductible, et elle prend une minute :

```bash
# Canal 1 — ce que la signature du binaire déclare (codeberg.org)
curl -fLO https://codeberg.org/forgejo/forgejo/releases/download/v15.0.7/forgejo-15.0.7-linux-amd64.asc
gpg --list-packets forgejo-15.0.7-linux-amd64.asc | grep 'issuer fpr'
#   → issuer fpr v4 3BF4E813F84812411DA01E5BC4186DF66F4B6750

# Canal 2 — ce que le projet publie (openpgpkey.forgejo.org)
fj key --fetch
#   → empreinte : EB114F5E6C0DC2BCDD183550A4B61A2DC5923710
gpg --show-keys --with-colons ct/RELEASE-KEY.asc | grep ^fpr
#   → …3BF4E813F84812411DA01E5BC4186DF66F4B6750  (sous-clé)
```

Les deux concordent : la clé qui a signé `v15.0.7` est une **sous-clé** de
`Forgejo Releases <release@forgejo.org>`, dont l'empreinte principale est
`EB114F5E…3710`. C'est cette empreinte principale qui est épinglée — les
sous-clés de signature tournent, l'identité du projet non.

### Ce que l'épinglage protège, et ce qu'il ne protège pas

**Il protège les mises à jour.** Si la clé de signature change — que ce soit
le projet qui en change ou quelqu'un qui substitue la sienne — le déploiement
REFUSE et le dit :

```
la clé récupérée ne correspond PAS à l'empreinte épinglée.
         épinglée : AAAA1111… (celle qu'on avait approuvée)
         trouvée(s) : BBBB2222… (celle que la source donne aujourd'hui)
         Rien n'est installé. Soit le projet a changé de clé de
         signature […], soit la source n'est pas celle qu'on croit.
```

**Il ne protège pas l'amorçage.** Si la toute première récupération est
compromise, on épingle la mauvaise clé et on la vérifie fidèlement ensuite.
C'est le défaut connu de ce modèle, et il est assumé.

Pour durcir l'amorçage — **facultatif, une minute** — comparer l'empreinte que
`fj key --fetch` affiche à celle que le projet annonce **ailleurs que sur la
page de téléchargement** : notes de version, documentation, salon Matrix. Si
les deux concordent, un attaquant aurait dû compromettre deux canaux.

### Le trousseau dédié

`fj deploy` importe la clé dans `/var/lib/fjtool/forgejo-release.gpg`, jamais
dans celui de root : y importer une clé de publication la rendrait de confiance
pour tout ce que root vérifie ensuite, bien au-delà de Forgejo.

```bash
gpg --no-default-keyring --keyring /var/lib/fjtool/forgejo-release.gpg --list-keys
```

### Le téléchargement se fait sur le NŒUD

Et non dans le conteneur. Trois raisons, la troisième suffit seule :

- le conteneur est la source de vérité ; lui donner un accès sortant en plus de
  son accès entrant élargit sa surface pour rien ;
- la vérification demande `gpg` et un trousseau, qui n'ont rien à faire là-bas ;
- **ce qui n'a pas été vérifié ne doit jamais toucher le disque du conteneur.**
  Télécharger dedans puis vérifier dedans, c'est déjà avoir écrit l'artefact non
  vérifié à l'endroit où il sera exécuté.

Le cache du nœud est `/var/cache/fjtool/`. Il survit aux redémarrages exprès :
reposer le même octet-pour-octet doit être possible sans retélécharger.

### Quand la vérification échoue

```
signature GPG NON vérifiée pour forgejo-15.0.3-linux-amd64 — rien ne sera installé
```

**Ne rien installer, et ne pas contourner.** L'artefact est supprimé du cache
par le code lui-même : le laisser en place ferait qu'un second passage le
trouverait « déjà là ». Dans l'ordre :

1. Vérifier que `ct/RELEASE-KEY.asc` est bien la clé de publication Forgejo, et
   non une clé périmée ou remplacée par erreur.
2. Rejouer `fj deploy` : un `.asc` tronqué par une coupure réseau donne le même
   message qu'une vraie signature invalide.
3. Si l'échec persiste avec une clé confirmée : **c'est un incident**. Ne pas
   installer, chercher pourquoi.

Une divergence de **somme** sans échec de signature est presque toujours un
téléchargement tronqué — mais le remède est le même : rejouer, ne pas forcer.

### Changer de version, le jour venu

1. Lire les notes de publication de la version visée. Chercher explicitement
   les migrations de schéma et les changements de configuration.
2. **Jouer une sauvegarde** : `fj backup`, puis un `vzdump` du CT.
3. `fj version --resolve` (dans la branche) ou éditer `ct/VERSION` à la main
   (changement de branche — c'est une décision, elle se commite avec sa raison).
4. `fj deploy --dry-run`, lire ce qui serait fait.
5. `fj deploy`.
6. `fj deploy --status` : le contrôle « version servie » doit rendre OK.

---

## 5. Arborescence et configuration

| Chemin | Propriétaire | Mode | Contenu |
|---|---|---|---|
| `/opt/forgejo/` | `root:root` | 755 | le binaire — ce que l'unité lance |
| `/usr/local/bin/forgejo` | — | lien | confort humain uniquement |
| `/etc/forgejo/` | `root:git` | 750 | `app.ini` |
| `/etc/forgejo/app.ini` | `root:git` | **640** | configuration, sans secret |
| `/etc/forgejo/secrets/` | `root:git` | **700** | les quatre secrets |
| `/var/lib/forgejo/` | `git:git` | 750 | dépôts, LFS, pièces jointes, sessions |
| `/var/backups/forgejo/` | — | — | volume `mp2`, les dumps |
| `/etc/forgejo-git/` | — | **ro** | le dépôt monté |

Les trois attributs comptent **ensemble**. `/etc/forgejo/secrets` en 0755
laisserait n'importe quel processus du conteneur lire la clé qui chiffre les
jetons d'accès, et rien ne le signalerait. `fj deploy` vérifie mode et
propriétaire dans le même aller-retour que l'existence.

### `app.ini` est une COPIE, pas un symlink

Contrairement aux fichiers de PostgreSQL, qui sont liés au montage et suivent
un `git pull` seuls. La raison est en [§ 7](#7-les-secrets).

Conséquence pratique : **un `git pull` ne suffit pas** à changer `app.ini` dans
le conteneur, il faut rejouer `fj deploy`. C'est le geste normal de toute
façon.

### Le montage est en lecture seule, et c'est vérifié

`ro=1` dans `pct config` dit ce qui a été **demandé** ; `fj deploy` lit
`/proc/mounts` dans le conteneur pour savoir ce qui a été **obtenu**. Les deux
divergent quand un `pct set` est passé sans redémarrage — un `mpN` n'est relu
qu'au démarrage.

Ce n'est pas une précaution de rangement : ce montage porte `app.ini` **et**
`ct/VERSION`. S'il était accessible en écriture, une instance compromise
réécrirait sa propre configuration et son propre épinglage.

Le contrôle se fait par **lecture**, jamais en tentant une écriture : une
protection se lit dans le refus qu'elle produit, elle ne s'éprouve pas en
écrivant.

---

## 6. Routage Traefik

Deux chemins, un seul nom d'hôte.

| Protocole | EntryPoint | Route |
|---|---|---|
| HTTPS | `websecure` (443) | `Host(forgejo.lan.wittner.tech)` → `http://192.168.1.57:3000` |
| SSH | `ssh` (2222) | `HostSNI(*)` → `192.168.1.57:2222` |

Fichiers : [`pve-ysera/traefik/dynamic/forgejo.yaml`](../../../pve-ysera/traefik/dynamic/forgejo.yaml)
et l'entryPoint `ssh` dans [`pve-ysera/traefik/traefik.yaml`](../../../pve-ysera/traefik/traefik.yaml).
Traefik surveille son répertoire dynamique (`watch: true`) : la route HTTP est
prise en compte sans redémarrage. **L'entryPoint, lui, est statique** — ajouter
`ssh` demande de redémarrer Traefik.

### Pourquoi le serveur SSH interne de Forgejo

`START_SSH_SERVER = true` : c'est le serveur SSH **en Go**, embarqué dans
Forgejo, qui écoute sur 2222 — et non le `sshd` du conteneur. Deux raisons :

- les clés publiques des utilisateurs restent gérées par Forgejo seul, et ne
  touchent jamais l'`authorized_keys` du compte d'administration ;
- le port 22 du conteneur reste à l'administration humaine.

### Pourquoi `HostSNI(*)`

SSH n'émet pas de SNI : il n'y a rien à router sur le nom d'hôte. C'est
l'entryPoint — donc le port — qui fait la sélection, et lui seul. Un
`HostSNI` nommé ne matcherait jamais.

### `passHostHeader: true` est obligatoire

Forgejo construit ses URL de redirection à partir de l'en-tête `Host`. Sans
lui, une connexion réussie renvoie vers `http://192.168.1.57:3000/` et le
navigateur sort du TLS — symptôme typique : « je me connecte, et je me
retrouve sur une page en clair avec une adresse IP ».

### `REVERSE_PROXY_TRUSTED_PROXIES`

**L'IP de Traefik, et elle seule. Jamais `*`.** Avec un joker, n'importe quel
client du LAN se déclare n'importe quelle adresse par un en-tête
`X-Forwarded-For` : les journaux d'audit et les limitations par IP ne veulent
alors plus rien dire.

Tant que `ct/app.ini` porte le marqueur `@@TRAEFIK_IP@@`, `fj deploy` refuse de
rendre un bilan vert et dit quoi faire.

### Le clone SSH ne dépend pas plus de Traefik que le HTTPS

Les deux passent par le CT 201. Cela n'ajoute **aucune** dépendance nouvelle à
la source de vérité — et si Traefik est absent, le clone reste possible en
visant directement le conteneur (`ssh://git@192.168.1.57:2222/…`, ou en HTTP
sur `http://192.168.1.57:3000/`). Voir [PRA](PRA.md).

---

## 7. Les secrets

Quatre fichiers, dans `/etc/forgejo/secrets/`, en `0640 root:git` :

| Fichier | Clé d'`app.ini` | Ce qu'il fait |
|---|---|---|
| `secret_key` | `SECRET_KEY_URI` | **chiffre la base** : jetons d'accès, secrets 2FA, mots de passe des miroirs |
| `internal_token` | `INTERNAL_TOKEN_URI` | authentifie les appels internes de Forgejo à lui-même |
| `oauth2_jwt_secret` | `[oauth2] JWT_SECRET_URI` | signe les jetons OAuth2 |
| `lfs_jwt_secret` | `LFS_JWT_SECRET_URI` | signe les jetons LFS |

### Pourquoi quatre, et pas deux

`SECRET_KEY` et `INTERNAL_TOKEN` sont ceux qu'on cite toujours. Mais Forgejo
**génère aussi** `JWT_SECRET` et `LFS_JWT_SECRET` s'ils manquent — et il les
écrit dans `app.ini` pour les y ranger.

Sur une configuration versionnée, cette écriture est exactement ce qu'on ne
veut pas. Et le montage étant en lecture seule, elle **échoue** — sans arrêter
le service. Forgejo continue alors avec des secrets tirés en mémoire, qui
changent à chaque redémarrage : sessions invalidées, jetons cassés, et aucune
erreur visible ailleurs que dans quelques lignes de journal.

C'est pour cela que :

- les **quatre** sont pré-déposés ;
- `app.ini` est une **copie** et non un symlink, ce qui rend au moins le
  fichier réparable sans toucher au montage ;
- un contrôle (`journal de forgejo`) relit le journal du service et **cherche
  explicitement** ce symptôme.

### Les générer

```bash
fj deploy --secrets
```

Ils sont produits par **le binaire lui-même** (`forgejo generate secret …`) et
non par un `head -c 32 /dev/urandom` maison : `INTERNAL_TOKEN` n'est pas une
chaîne aléatoire, c'est un jeton signé dont Forgejo attend une forme précise.

Ils **s'affichent une fois**, et il faut les ranger dans OpenBao immédiatement.

> **À confirmer au premier déploiement réel.** Les quatre clés `*_URI`
> d'`app.ini` et les quatre noms passés à `forgejo generate secret`
> (`SECRET_KEY`, `INTERNAL_TOKEN`, `JWT_SECRET`, `LFS_JWT_SECRET`) ont été
> écrits d'après la forme héritée de Gitea, sans avoir pu être confrontés à la
> documentation de la 15.0 au moment de la rédaction.
>
> Ce qu'on observe si l'un d'eux a changé de nom : Forgejo **ignore** la clé
> inconnue, considère le secret comme absent, le génère lui-même, et tente
> d'écrire `app.ini`. Le symptôme est donc celui du [cas C du
> PRA](PRA.md#cas-c--secrets-éphémères-sessions-qui-sautent-jetons-cassés) —
> et le contrôle `journal de forgejo` de `fj deploy` le rend visible dès le
> premier `--status`.
>
> La forme exacte se lit dans `forgejo generate secret --help` et dans la
> configuration d'exemple livrée avec le binaire :
> ```bash
> pct exec 400 -- /opt/forgejo/forgejo generate secret --help
> ```

> **Sans `secret_key`, une base restaurée reste chiffrée et illisible.** C'est
> le scénario de reprise qui échoue le plus silencieusement : le dump se
> restaure, le service démarre, et tous les jetons et secrets 2FA sont
> définitivement perdus. Le seul remède est de l'avoir rangé avant.

**Un secret qui existe n'est jamais retouché.** Rejouer `fj deploy --secrets`
sur une instance complète ne fait rien. Il n'y a pas de rotation par accident,
parce qu'une rotation de `SECRET_KEY` ne « renouvelle » rien : elle rend
illisible tout ce qui a été chiffré avant.

### Les reposer depuis OpenBao

Si le conteneur est reconstruit, les secrets viennent d'OpenBao et non d'une
nouvelle génération :

```bash
for nom in secret_key internal_token oauth2_jwt_secret lfs_jwt_secret; do
  bao kv get -field="$nom" homelab/forgejo \
    | pct exec 400 -- sh -c 'umask 027 && cat > "/etc/forgejo/secrets/$1"' sh "$nom"
  pct exec 400 -- chown root:git "/etc/forgejo/secrets/$nom"
done
pct exec 400 -- systemctl restart forgejo
```

---

## 8. Durcissement git

Trois réglages, posés `--system` (donc dans `/etc/gitconfig`) :

```
transfer.fsckObjects = true
receive.fsckObjects  = true
fetch.fsckObjects    = true
```

C'est l'équivalent manuel d'un durcissement arrivé en v16. On ne l'attend pas
deux ans.

| Réglage | Ce qu'il couvre |
|---|---|
| `transfer` | le parapluie |
| `receive` | ce qui **entre par un push** — le chemin par lequel un utilisateur du homelab peut écrire |
| `fetch` | ce qui entre par un **miroir tiré** depuis l'extérieur |

Les trois, et pas deux : en poser deux laisse la troisième porte ouverte, et
c'est toujours celle-là qui sert.

Un objet incohérent accepté aujourd'hui est un dépôt qu'on ne peut plus cloner
demain. Pour une source de vérité, c'est la panne qui coûte le plus cher, parce
qu'elle ne se voit qu'au moment de s'en servir.

Vérifier :

```bash
pct exec 400 -- git config --system --list | grep fsck
```

---

## 9. Sauvegardes

### Deux moitiés, et il faut les deux

```
la BASE      → fj-backup.timer, 02:45, dans le CT      → /var/backups/forgejo/
les DÉPÔTS   → vzdump du CT 400                        → stockage de sauvegarde
```

**Restaurer Forgejo demande LES DEUX**, pris à des instants proches. Une base
qui référence un dépôt absent du disque — ou l'inverse — donne une instance qui
démarre et se comporte n'importe comment.

### Pourquoi les dépôts ne sont pas dans la sauvegarde logique

`/var/lib/forgejo` pèse des dizaines de gigaoctets (dépôts, LFS, pièces
jointes). Le tarer chaque nuit à côté d'une base de quelques centaines de
mégaoctets remplirait le volume `mp2` en quelques jours, pour une redondance
que `vzdump` assure déjà.

### Les dépôts partent par vzdump

**À faire une fois** : ajouter le CTID 400 à la sélection de sauvegarde du
nœud, et à la liste de ce qui part vers GCS Nearline. Tant que ce n'est pas
fait, **les dépôts n'ont aucune copie**.

Le volume `mp2` porte `backup=0` : les `vzdump` du CT ne l'embarquent donc pas.
Sans ce drapeau, chaque sauvegarde du conteneur emporterait tous les dumps
précédents, et doublerait de taille à chaque passage.

### Le manifeste, et à quoi il sert

Chaque instantané porte un `MANIFEST` :

```
STAMP=20260821-024500
FORGEJO_VERSION=v15.0.3
DATABASE=forgejo
DUMP_BYTES=41943040
REPOS_COUNT=12
REPOS_BYTES=987654321
REPOS_LAST_MTIME=1755000000
```

Les trois lignes `REPOS_*` décrivent l'arborescence des dépôts **au moment du
dump**. En reprise, c'est ce qui permet de dire si le `vzdump` retenu
correspond au dump retenu — au lieu de l'espérer.

Trois nombres et non une empreinte : hacher des dizaines de gigaoctets à 2h45
coûterait plus que la sauvegarde elle-même, et ces trois-là suffisent à
répondre « non » quand c'est non, ce qui est le sens utile.

### Atomicité

Tout est écrit dans `<stamp>.part/`, renommé en `<stamp>/` seulement si
l'exécution va au bout. Un répertoire présent est donc, **par construction**,
une sauvegarde complète. Une exécution interrompue ne laisse rien qu'une copie
hors-site pourrait prendre pour bonne.

### Rétention

14 jours par défaut, et **jamais le dernier instantané** — quelle que soit la
rétention. Une rétention réglée à 0 par erreur, ou une horloge qui saute,
effacerait sinon tout ce qui reste. Une source de vérité sans aucune
sauvegarde est le seul état dont on ne se relève pas.

### Les ACL ne sont pas dans le dump

Ni dans le dump, ni dans un `globals.sql`. Après une restauration, `PUBLIC`
retrouve `CONNECT` sur la base et **l'isolation disparaît en silence** : la
base remonte, tout a l'air normal.

Le remède est de rejouer `ct/init.sql`, qui est idempotent — c'est le même
fichier qui pose l'isolation et qui la rétablit, donc il n'existe pas deux
définitions de ce qu'« isolé » veut dire :

```bash
pct exec 400 -- sudo -u postgres psql -v ON_ERROR_STOP=1 \
     -f /etc/forgejo-git/init.sql
pct exec 400 -- sudo -u postgres psql -c '\l forgejo'   # doit afficher =T/... forgejo=CTc/...
```

`fj deploy` contrôle cette ACL **deux fois** : une fois en section P, une fois
en fin de parcours après que Forgejo a créé ses tables. Seule la seconde
répond à la question telle qu'elle se pose vraiment.

---

## 10. Copie hors-site vers GCS

### Installation

La clé du compte de service est un **secret** : elle n'est pas dans le dépôt et
`fj deploy` ne peut pas la fabriquer. Elle se dépose à la main :

```bash
install -m 600 -D /chemin/vers/cle.json /root/.config/rclone/pgsql-backups.json
```

Le bucket est celui des sauvegardes PostgreSQL, **réutilisé à dessein** : mêmes
règles de cycle de vie (Nearline à 30 j, Coldline à 90 j, suppression à 365 j),
même compte de service aux droits volontairement incomplets. Son nom parle de
« pgsql » pour des raisons historiques ; le sous-chemin distingue les services :

```
gs://homelab-pgsql-backups-dc93212a/pve-eranikus/forgejo/<stamp>/
```

### Droits volontairement incomplets

Le compte de service est `objectViewer` + `objectCreator` : il liste, lit et
crée, **il n'écrase ni ne supprime**. Un nœud compromis ne peut donc pas
détruire l'historique.

Conséquences directes, et elles ne sont pas des limitations à contourner :

- le transfert est en `--ignore-existing` ;
- `core.commands.Rclone` n'expose ni `sync` ni `delete` — cette absence **est**
  la garantie ;
- un objet distant qui diverge est **signalé**, jamais réparé d'ici.

### Le piège de l'accès uniforme (UBLA)

Sans `bucket_policy_only = true` dans `rclone.conf`, rclone joint une ACL
héritée à chaque objet et le transfert échoue :

```
Error 400: Cannot insert legacy ACL for an object when uniform bucket-level
access is enabled
```

Zéro octet écrit. Constaté sur `pve-eranikus` le 20 août 2026. Le drapeau
`--gcs-bucket-policy-only` est posé **en plus** de la ligne de configuration, à
dessein : le code doit marcher sur une configuration reconstruite à la va-vite.

### Objet distant divergent

```
code 3 — au moins un objet distant diverge, intervention humaine
```

Le compte de service ne peut pas écraser : personne ne peut réparer cela depuis
le nœud, et surtout pas en boucle. C'est presque toujours un objet partiel
laissé par un transfert interrompu.

Le remède demande un compte **humain** ayant le droit de supprimer :

```bash
gcloud storage rm gs://homelab-pgsql-backups-dc93212a/pve-eranikus/forgejo/<stamp>/<fichier>
fj offsite        # qui le retransférera
```

### Codes de retour

| Code | Sens |
|---|---|
| 0 | tout est en ligne |
| 1 | environnement inutilisable — rclone, clé, bucket, aucune sauvegarde |
| 2 | au moins un transfert a échoué (sera retenté demain) |
| 3 | au moins un objet distant diverge — **intervention humaine** |
| 130 | interrompu par signal |

Une faute de frappe sort en **1**, pas en 2 : sinon systemd la consignerait
comme une panne de transfert, et elle se lirait comme telle trois semaines plus
tard.

---

## 11. Miroir sortant vers GitHub

**Sortant uniquement.** Un miroir *push* ne recrée aucune dépendance entrante :
Forgejo pousse vers GitHub, GitHub ne sait rien de Forgejo et ne peut rien lui
demander. Cela laisse un chemin de reprise si l'instance meurt — et c'est tout
ce qu'on lui demande.

À configurer **par dépôt**, au minimum sur les manifests ArgoCD.

1. Créer sur GitHub un dépôt **privé** vide, et un jeton d'accès restreint à ce
   dépôt, avec le seul droit d'écriture sur le contenu.
2. Le ranger dans OpenBao.
3. Dans Forgejo : *Paramètres du dépôt → Miroirs → Ajouter un miroir push*,
   URL `https://github.com/<org>/<dépôt>.git`, identifiants = le jeton.
4. Vérifier que la première synchronisation passe, puis que l'intervalle est
   celui voulu (8 h convient).

> Le jeton est stocké **chiffré par `SECRET_KEY`** dans la base. Un conteneur
> reconstruit sans le bon `secret_key` ne peut plus le déchiffrer, et les
> miroirs échouent en silence — une raison de plus de l'avoir rangé
> ([§ 7](#7-les-secrets)).

Ce miroir n'est **pas** une sauvegarde : il ne porte ni les tickets, ni les
demandes d'ajout, ni les comptes, ni les clés. Il porte les objets git, ce qui
est exactement ce dont ArgoCD a besoin pour repartir.

---

## 12. Vérifications de recette

À jouer après la première pose, et à rejouer après tout changement de version.
Les six premières sont automatisées — `fj deploy --status` les rend toutes ;
les deux dernières demandent une main.

| # | Ce qu'on vérifie | Comment |
|---|---|---|
| 1 | La version installée est bien une 15.0.x | `pct exec 400 -- /opt/forgejo/forgejo --version` |
| 2 | Aucune écoute TCP côté PostgreSQL | `pct exec 400 -- ss -lntp` — rien sur 5432 |
| 3 | `REVOKE CONNECT` toujours effectif | `pct exec 400 -- sudo -u postgres psql -c '\l forgejo'` |
| 4 | L'inscription publique est refusée | ouvrir `https://forgejo.lan.wittner.tech/user/sign_up` — doit refuser |
| 5 | Les trois `fsck` sont posés | `pct exec 400 -- git config --system --list \| grep fsck` |
| 6 | Le montage est en lecture seule vu du CT | `pct exec 400 -- grep forgejo-git /proc/mounts` — doit porter `ro` |
| 7 | **Le CT remonte seul après redémarrage** | `pct reboot 400`, attendre, puis `fj status` |
| 8 | **Clone HTTPS et SSH depuis l'extérieur** | voir ci-dessous |

### 7. Le redémarrage

C'est la vérification qu'aucun contrôle automatique ne peut faire à votre
place : elle demande de couper pour de bon.

```bash
pct reboot 400
# attendre le retour, puis :
fj status
```

Les quatre maillons doivent répondre, **sans aucune intervention**. Un service
qui ne remonte pas seul est un service qui ne remontera pas après une coupure
de courant — c'est-à-dire exactement quand on en a besoin.

### 8. Le clone, dans les deux protocoles

Depuis une machine du LAN, **pas depuis le nœud** : le but est d'éprouver le
chemin complet, Traefik compris.

```bash
git clone https://forgejo.lan.wittner.tech/<org>/<dépôt>.git
git clone ssh://git@forgejo.lan.wittner.tech:2222/<org>/<dépôt>.git
```

Le second échoue tant que l'entryPoint `ssh` de Traefik n'a pas été pris en
compte — il est **statique**, donc il demande un redémarrage de Traefik
([§ 6](#6-routage-traefik)).
