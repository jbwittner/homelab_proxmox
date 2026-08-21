# Runbook — CT Forgejo (`pve-eranikus`, CTID 400)

Le détail : création, conception, pièges. Ce qu'on tape au quotidien est dans
le [README](../README.md).

**Les numéros de section sont stables.** Des messages d'erreur du code et le
`Documentation=` des unités systemd y renvoient par numéro. Déplacer une
section, c'est aussi corriger ces renvois-là.

## Sommaire

- [0. Retrait de l'instance 16.0 existante](#0-retrait-de-linstance-160-existante)
- [0 bis. Repartir de zéro après un déploiement co-localisé](#0-bis-repartir-de-zéro-après-un-déploiement-co-localisé)
- [1. Création du conteneur](#1-création-du-conteneur)
- [2. Déploiement depuis l'hôte : `fj deploy`](#2-déploiement-depuis-lhôte--fj-deploy)
- [3. La base, locataire du CT 200](#3-la-base-locataire-du-ct-200)
- [4. La version épinglée](#4-la-version-épinglée)
- [5. Arborescence et configuration](#5-arborescence-et-configuration)
- [6. Routage Traefik](#6-routage-traefik)
- [7. Les secrets](#7-les-secrets)
- [8. Durcissement git](#8-durcissement-git)
- [9. Sauvegardes — ce que ce conteneur ne fait pas](#9-sauvegardes--ce-que-ce-conteneur-ne-fait-pas)
- [10. Restaurer](#10-restaurer)
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

## 0 bis. Repartir de zéro après un déploiement co-localisé

**À jouer si le CT 400 a été déployé avec la version où PostgreSQL était
co-localisé.** Reconnaissable à ceci :

```bash
pct exec 400 -- pg_lsclusters          # un cluster local → version périmée
pct config 400 | grep mp2              # un volume de sauvegarde → idem
systemctl list-unit-files 'fjbk-*'     # sur le NŒUD
```

### Ce qui presse, et ce qui ne presse pas

**Ce qui presse est sur le NŒUD, pas dans le conteneur.** Les unités
`fjbk-offsite` ont pu être armées. Leur `ExecStart` invoque une sous-commande
`offsite` qui **n'existe plus** dans le parseur : elles échoueraient toutes les
nuits à **03:50**, dans une plage horaire où personne ne regarde.

Bonne nouvelle : `fj deploy` s'en charge désormais lui-même — la section H
désarme puis retire les deux unités et leur drop-in. Un `--dry-run` le montre
avant de le faire :

```bash
cd /root/homelab_proxmox && git pull
pve-eranikus/forgejo/fj deploy --dry-run
```

**Le lanceur du dépôt, pas `fj` du `PATH`** : la copie installée date du
déploiement précédent et ne connaît pas encore ces retraits — voir
[§ 2](#amorçage--quand-la-copie-installée-est-en-retard).

Le conteneur, lui, ne presse pas : il ne casse rien tant qu'on n'y touche pas.

### Le chemin recommandé : détruire et recréer

Le CT 400 d'un déploiement co-localisé **ne contient rien qui vaille d'être
sauvé** — aucun dépôt n'a encore été poussé, et les secrets seront régénérés.
Le reconstruire coûte quelques minutes et évite tout état intermédiaire.

**Vérifier d'abord qu'il est bien vide**, comme en [§ 0](#0-retrait-de-linstance-160-existante) :

```bash
pct exec 400 -- find /var/lib/forgejo/repositories -maxdepth 2 -name '*.git' 2>/dev/null
```

Aucune ligne ? Alors :

```bash
# 1. Le nœud d'abord — c'est ce qui échouerait cette nuit
pve-eranikus/forgejo/fj deploy --no-container

# 2. Détruire le conteneur
pct stop 400
pct set 400 --protection 0
pct destroy 400 --purge

# 3. Le recréer — § 1, et SANS postgresql cette fois
#    (le §1 à jour n'installe que « sudo »)

# 4. Créer le locataire sur le CT 200, si ce n'est pas déjà fait
pve-eranikus/pgsql/pg deploy --tenant forgejo
#    puis déposer le mot de passe — § 3

# 5. Déployer
pve-eranikus/forgejo/fj deploy --secrets
fj status
```

> `fj deploy --no-container` à l'étape 1 : il fait tout le travail du nœud —
> dont les retraits — **sans toucher au CT**, qu'on s'apprête à détruire de
> toute façon. Un drapeau `--no-*` ne désactive jamais un contrôle, seulement
> une pose : le bilan reste complet.

### Si le conteneur doit être gardé

Par exemple parce que des dépôts y ont déjà été poussés. Le nettoyage se fait
alors à la main, **dans cet ordre**, et rien de tout ceci n'est automatisé —
ce sont des gestes destructifs sur des données :

```bash
# a. Arrêter Forgejo, il tient une connexion à la base locale
pct exec 400 -- systemctl stop forgejo

# b. Désarmer et retirer la sauvegarde locale
pct exec 400 -- systemctl disable --now fj-backup.timer
pct exec 400 -- rm -f /etc/systemd/system/fj-backup.service \
                      /etc/systemd/system/fj-backup.timer
pct exec 400 -- systemctl daemon-reload

# c. Les symlinks de configuration PostgreSQL pointent vers des fichiers
#    SUPPRIMÉS du dépôt : les retirer avant de toucher au cluster
pct exec 400 -- sh -c 'rm -f /etc/postgresql/*/main/pg_hba.conf \
                             /etc/postgresql/*/main/pg_ident.conf \
                             /etc/postgresql/*/main/conf.d/10-forgejo.conf'

# d. Le cluster local — DESTRUCTIF. Relever d'abord ce qu'il contient.
pct exec 400 -- pg_lsclusters
pct exec 400 -- apt-get purge -y postgresql postgresql-* 
pct exec 400 -- apt-get install -y postgresql-client

# e. Le moteur poussé dans le CT, que plus rien n'appelle
pct exec 400 -- rm -rf /usr/local/lib/fjtool /usr/local/bin/fj

# f. Le volume de sauvegarde — DESTRUCTIF, et il porte peut-être des dumps
pct config 400 | grep mp2          # relever le volid AVANT
pct stop 400
pct set 400 --delete mp2           # ne détruit pas le volume, le détache
pct start 400
#    Le volume détaché reste dans le stockage : le supprimer une fois
#    certain qu'il ne sert plus (pvesm free <volid>).
```

Puis reprendre au point 4 ci-dessus. **`fj deploy` refusera tant que le
locataire du CT 200 n'existe pas** — c'est le maillon « connexion à la base ».

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

Puis `sudo`, seul paquet dont `fj deploy` a besoin pour travailler dedans :

```bash
pct exec 400 -- apt-get update
pct exec 400 -- apt-get install -y sudo
```

**Pas de `python3` dans ce conteneur** : `fj` est un outil de nœud, rien n'y
est poussé. Le reste — paquets, utilisateur, arborescence, binaire, unités —
est le travail de `fj deploy` ([§ 2](#2-déploiement-depuis-lhôte--fj-deploy)).

### Pourquoi ces valeurs

| Choix | Raison |
|---|---|
| **32 Go** de disque | Les 10 Go du script communautaire sont trop justes dès qu'il y a du LFS ou un miroir. Agrandir plus tard demande un `pct resize` et une extension de système de fichiers ; le faire maintenant coûte zéro. |
| **non privilégié** | Forgejo exécute des hooks git écrits par les utilisateurs des dépôts. |
| **`nesting=1`** | Obligatoire sur Debian 13 — voir ci-dessous. |
| **IP statique** | Une source de vérité ne dépend pas du DHCP. Un bail perdu, et Traefik route vers le vide. |
| **`onboot=1` + `startup order=1`** | Elle doit être debout **avant** que le reste ne cherche à se réconcilier. `onboot` dit *s'il* démarre, `startup` dit *dans quel ordre* : les deux sont indépendants, et un CT avec un ordre mais sans `onboot` ne démarre jamais. **Attention** : le CT 200 porte la base, il doit donc avoir un ordre INFÉRIEUR — voir [§ 3](#lordre-de-démarrage-au-boot-du-nœud). |
| **2 Go / 2 vCPU** | Une instance à quelques utilisateurs. La base tourne ailleurs (CT 200), donc ce conteneur ne porte que Forgejo lui-même. |

### Le piège du nesting

Sans `nesting=1`, les unités qui montent un tmpfs pour les *credentials*
systemd — ce que fait `forgejo.service` avec `PrivateTmp=true` — échouent en :

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
A  prérequis du conteneur      protection, nesting, onboot, mp1, startup
   ── barrière : le CT redémarre ici, et nulle part ailleurs
D  outillage du nœud           fj, arbre d'import, CTID consigné, gnupg
B  pose dans le conteneur      paquets, utilisateur git, arborescence, unité
   ── barrière : systemd relit ses unités
V  installation binaire        version épinglée, clé, téléchargement vérifié
P  la base (CT 200)            mot de passe déposé, connexion ÉPROUVÉE
B  app.ini                     RENDU, avec le mot de passe substitué
G  les secrets                 AVANT le premier démarrage
   ── barrière
B  le service                  forgejo
H  ce qui ne doit pas être là  orphelins, automatismes de mise à jour
C  contrôles                   en dernier, sinon ils répondent sur l'état d'avant
```

**Ce plan ne sauvegarde rien et ne copie rien hors-site.** La base est un
locataire du CT 200 ; les dépôts partent par `vzdump`. Voir
[§ 9](#9-sauvegardes--ce-que-ce-conteneur-ne-fait-pas).

### Amorçage : quand la copie installée est en retard

**Le piège qui se reproduira à chaque changement de `fj` lui-même.**

Il y a deux `fj` sur le nœud, et ils ne sont pas au même niveau :

| Invocation | Ce qui s'exécute |
|---|---|
| `fj …` | `/usr/local/sbin/fj` + `/usr/local/lib/fjtool` — la copie **installée** |
| `./fj …` ou `pve-eranikus/forgejo/fj …` | le lanceur **du dépôt**, avec `fjtool/` du dépôt |

La copie installée n'est rafraîchie **que par `fj deploy`** — c'est l'étape
« arbre d'import (hôte) ». Donc après un `git pull` qui change `fj`, la copie
installée est encore l'ancienne, et `fj` du `PATH` exécute du vieux code.

Le symptôme est déroutant : `git pull` dit « Already up to date », le dépôt
porte bien le correctif, et la commande se comporte toujours comme avant.

**Toujours amorcer par le lanceur du dépôt :**

```bash
cd /root/homelab_proxmox && git pull
pve-eranikus/forgejo/fj deploy --dry-run     # le lanceur du DÉPÔT
pve-eranikus/forgejo/fj deploy
```

Après quoi `/usr/local/lib/fjtool` est à jour et `fj` du `PATH` redevient
équivalent. Vérifier laquelle des deux on exécute, en cas de doute :

```bash
command -v fj                                    # /usr/local/sbin/fj
diff -rq /usr/local/lib/fjtool/fjtool \
         /root/homelab_proxmox/pve-eranikus/forgejo/fjtool
```

Aucune sortie = les deux sont au même niveau.

### Les ordres qui comptent

**Les secrets avant le premier démarrage.** C'est l'ordre le plus important du
plan. Démarrer d'abord laisserait Forgejo générer ses secrets lui-même et
tenter de réécrire un `app.ini` qu'il ne peut pas écrire — voir
[§ 7](#7-les-secrets).

**Le mot de passe avant `app.ini`.** La configuration le CONTIENT : la rendre
avant que le secret soit déposé produirait un `app.ini` portant le marqueur
`@@DB_PASSWORD@@` en guise de mot de passe, et un échec d'authentification qui
ne dirait pas pourquoi.

**L'outillage du nœud avant la section V.** C'est le nœud qui télécharge et qui
vérifie ; sans `gnupg`, il n'y a rien à vérifier.

**La connexion à la base avant le service.** Sinon le premier démarrage n'est
qu'une suite d'échecs d'authentification, que quelqu'un lira comme une panne de
Forgejo alors que c'est une ligne manquante dans le `pg_hba.conf` du CT 200.

**Les contrôles en dernier.** Un contrôle joué au milieu répond sur l'état
d'*avant* les poses qui le suivent.

### Ce qu'il ne fait pas

| Geste | Pourquoi il reste manuel |
|---|---|
| Créer le conteneur | Geste unique — [§ 1](#1-création-du-conteneur) |
| Créer le locataire de la base | Une seule définition de « locataire », et elle est au CT 200 — [§ 3](#3-la-base-locataire-du-ct-200) |
| Déposer le mot de passe de la base | C'est un secret, il vient d'OpenBao — [§ 3](#créer-le-locataire--sur-le-ct-200-pas-ici) |
| Épingler la clé de signature | `fj key --fetch`, joué une fois — [§ 4](#la-clé-de-publication) |
| Résoudre `ct/VERSION` | C'est une décision — [§ 4](#4-la-version-épinglée) |

---

## 3. La base, locataire du CT 200

Forgejo n'a pas de base à lui. Il est un **locataire du cluster mutualisé** du
CT 200, comme n'importe quel autre service du nœud.

### Pourquoi mutualisé, alors que le brief disait l'inverse

Le brief demandait un cluster **co-localisé** dans le CT 400, et son argument
était celui-ci : mutualiser créerait la chaîne `pve-eranikus → CT 200 →
Forgejo → ArgoCD → cluster`, où une panne d'un nœud n'hébergeant même pas
Forgejo bloquerait toute réconciliation GitOps.

**Cet argument est tombé le jour où Forgejo a rejoint `pve-eranikus`.** Les
deux conteneurs sont sur la même machine : ils tombent ensemble, et prétendre
le contraire serait se raconter une histoire. Ce qui restait — cycles de vie
distincts, rayon de panne d'un cluster partagé — ne valait pas un second
cluster PostgreSQL à maintenir, qui aurait de surcroît été en majeure **17**
(paquet Debian) face au **18** (PGDG) du CT 200.

Ce qu'on gagne en mutualisant :

- **une seule majeure PostgreSQL** sur le nœud, donc une seule migration à
  jouer le jour venu ;
- **la sauvegarde et le hors-site pour rien** : `pg-backup` dumpe déjà toutes
  les bases du cluster, `pgbk-offsite` les emporte déjà. Forgejo en hérite
  sans une ligne de code ;
- `pg restore forgejo`, `pg verify forgejo`, `pg list` — l'outillage de
  restauration existe et il est éprouvé.

Ce qu'on paie, et il faut le dire : **un mot de passe de base à faire vivre.**
La connexion passe du socket Unix en `peer` — où le noyau atteste l'identité —
au TCP en `scram-sha-256`. C'est exactement ce que la co-localisation évitait.

### Créer le locataire — sur le CT 200, pas ici

```bash
# Sur le nœud
pve-eranikus/pgsql/pg deploy --tenant forgejo
```

Le mot de passe s'affiche **une seule fois**. Le ranger dans OpenBao
immédiatement, puis le déposer dans le CT 400 :

```bash
bao kv get -field=db_password homelab/forgejo \
  | pct exec 400 -- sh -c 'umask 027 && cat > /etc/forgejo/secrets/db_password'
pct exec 400 -- chown root:git /etc/forgejo/secrets/db_password
```

Reste **un** geste manuel, celui que `pg deploy` ne fait pas : ajouter la ligne
du locataire dans le `pg_hba.conf` du CT 200, **avant** la règle `reject`, puis
rejouer `pg deploy`. Elle est déjà écrite :

```
hostssl   forgejo     forgejo       192.168.1.57/32         scram-sha-256
```

### Ce que `fj deploy` fait, et ne fait pas

Il ne crée **rien** dans la base. Il constate :

  - que `/etc/forgejo/secrets/db_password` existe, n'est pas vide, et est en
    `0640 root:git` ;
  - que la connexion **fonctionne réellement**, éprouvée depuis le CT 400 avec
    ce mot de passe, en SSL.

Deux outils qui créeraient la même base finiraient par la créer de deux façons
— les ACL d'un côté, pas de l'autre — et personne ne saurait laquelle fait foi.

### Les trois échecs, et ce qu'ils veulent dire

Aucun ne nomme sa cause. Le contrôle rend la première ligne du refus telle
quelle, parce que c'est elle qui tranche :

| Message | Cause | Remède |
|---|---|---|
| `no pg_hba.conf entry for host "192.168.1.57"` | la ligne du locataire manque, ou est après le `reject` | l'ajouter sur le CT 200, puis `pg deploy` |
| `password authentication failed for user "forgejo"` | le mot de passe déposé n'est pas celui du rôle | voir ci-dessous |
| `database "forgejo" does not exist` | le locataire n'a jamais été créé | `pg deploy --tenant forgejo` |

### Reposer le mot de passe du locataire

**Le cas le plus fréquent, et il surprend.** `pg deploy --tenant forgejo`
répond « forgejo existe — inchangé » et n'affiche aucun mot de passe. Ce n'est
pas un bug : il ne fait **jamais** tourner un secret déjà rangé dans OpenBao.
Rejouer un déploiement de routine ne doit pas invalider un mot de passe que
quelqu'un a noté quelque part.

Conséquence : si la base existait déjà — parce qu'un `--tenant` a été joué un
jour, ou parce qu'un déploiement précédent l'avait créée — vous n'obtiendrez
jamais le mot de passe par cette commande. Il faut soit le retrouver, soit en
poser un nouveau.

**Le retrouver**, si OpenBao l'a :

```bash
bao kv get -field=db_password homelab/forgejo \
  | pct exec 400 -- sh -c 'umask 027 && cat > /etc/forgejo/secrets/db_password'
pct exec 400 -- chown root:git /etc/forgejo/secrets/db_password
```

**En poser un nouveau**, sinon. La porte `peer` du CT 200 permet de le faire
sans connaître l'ancien — c'est exactement à ça qu'elle sert :

```bash
# Sur le nœud. Alphanumérique : rien à échapper nulle part, ni dans un
# .pgpass, ni dans app.ini, ni dans un gestionnaire de mots de passe.
NOUVEAU=$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)

# Le poser sur le rôle. Le SQL part sur l'entrée standard et la valeur par
# -v : c'est psql qui cite, donc aucun caractère ne peut changer le sens.
printf "ALTER ROLE forgejo PASSWORD :'p';\n" \
  | pct exec 200 -- sudo -u postgres psql -v ON_ERROR_STOP=1 -v p="$NOUVEAU" -q

# Le déposer dans le CT 400
printf '%s' "$NOUVEAU" \
  | pct exec 400 -- sh -c 'umask 027 && cat > /etc/forgejo/secrets/db_password'
pct exec 400 -- chown root:git /etc/forgejo/secrets/db_password

# LE RANGER DANS OPENBAO, puis l'effacer du shell
echo "$NOUVEAU"
unset NOUVEAU
```

Puis rejouer `fj deploy` : `app.ini` est **rendu** à partir de ce fichier, donc
il reprendra la nouvelle valeur tout seul.

> Le mot de passe traverse brièvement l'argv de `psql` (via `-v`). C'est le
> même compromis que `pg deploy --tenant`, et il est assumé : l'alternative —
> un fichier temporaire — laisserait le secret sur un disque. Ici il ne vit que
> le temps d'un appel, dans un `ps` que seul root peut lire.

### Les deux-points dans un mot de passe

La sonde de connexion écrit une ligne `.pgpass`, où **les deux-points séparent
les champs et l'antislash échappe**. Un mot de passe qui en contient casserait
la ligne, et le serveur répondrait `password authentication failed` — c'est-à-dire
exactement le message d'un mauvais mot de passe, alors que le secret serait
juste.

`fj` échappe donc les deux caractères avant d'écrire la ligne. Mais tant qu'à
faire, **s'en tenir à de l'alphanumérique** : c'est ce que produit
`pg deploy --tenant`, et ça évite le problème partout à la fois.

### L'ordre de démarrage au boot du nœud

Le CT 200 doit remonter **avant** le CT 400, sinon Forgejo démarre sur une base
injoignable. Ce n'est pas `forgejo.service` qui le garantit — la base n'est pas
dans son conteneur, un `After=` local ne dirait rien d'un service distant.
C'est `startup` de Proxmox :

```bash
pct config 200 | grep startup     # doit être un ordre INFÉRIEUR
pct config 400 | grep startup
```

Forgejo sait attendre sa base et réessaie ; l'ordre évite surtout un journal
rempli d'échecs au démarrage du nœud, que quelqu'un lirait comme une panne.

### L'isolation de la base

`REVOKE CONNECT … FROM PUBLIC` **n'est pas contrôlé par `fj`**. La base
appartient au CT 200, et l'outil qui en juge est le sien :

```bash
pg verify forgejo
```

Le redoubler ici demanderait d'analyser une sortie faite pour des humains — ce
que ce dépôt s'interdit — et donnerait deux définitions de « isolé », dont
l'une finirait par mentir. À jouer après toute restauration : **les ACL ne sont
ni dans le dump ni dans `globals.sql`**, elles disparaissent en silence.

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
2. **Jouer les DEUX sauvegardes** : `pg backup` sur le CT 200 pour la
   base, puis un `vzdump` du CT 400 pour les dépôts et le binaire actuel.
   C'est le vzdump qui permet de revenir en arrière si la migration de
   schéma se passe mal — elle est irréversible.
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
| `/etc/forgejo/app.ini` | `root:git` | **640** | configuration **rendue**, mot de passe de la base compris |
| `/etc/forgejo/secrets/` | `root:git` | **700** | les quatre secrets **et** `db_password` |
| `/var/lib/forgejo/` | `git:git` | 750 | dépôts, LFS, pièces jointes, sessions |
| `/etc/forgejo-git/` | — | **ro** | le dépôt monté |

Les trois attributs comptent **ensemble**. `/etc/forgejo/secrets` en 0755
laisserait n'importe quel processus du conteneur lire la clé qui chiffre les
jetons d'accès, et rien ne le signalerait. `fj deploy` vérifie mode et
propriétaire dans le même aller-retour que l'existence.

### `app.ini` est RENDU, pas copié

C'est la seule exception du dépôt, et elle tient à une valeur. `ct/app.ini` est
un **gabarit** : il porte le marqueur `@@DB_PASSWORD@@`, que `fj deploy`
remplace par le contenu de `/etc/forgejo/secrets/db_password`. Le fichier servi
ne peut donc pas exister dans le dépôt — un mot de passe n'y a pas sa place.

Le rendu se fait **entièrement dans le conteneur**, en un seul `sh -c` dont le
script est constant : le secret ne traverse ni un argv, ni un fichier du nœud.
Un `ps` pendant l'opération ne montre rien. Le mot de passe est passé à `awk`
par `-v`, donc il ne traverse jamais une expression rationnelle — aucun
caractère ne peut y changer le sens du remplacement.

La comparaison porte sur **l'empreinte du résultat**, pas sur le gabarit. C'est
ce qui permet à « zéro modification sur un état conforme » de tenir alors même
que le fichier servi n'est identique à aucun fichier du dépôt.

Conséquence pratique : **un `git pull` ne suffit jamais**, il faut rejouer
`fj deploy`. C'est le geste normal de toute façon.

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
- `app.ini` est **rendu** dans `/etc/forgejo`, en `root:git`, donc réparable
  sans toucher au montage ;
- un contrôle (`journal de forgejo`) relit le journal du service et **cherche
  explicitement** ce symptôme.

Un cinquième fichier vit dans le même répertoire sans être de la même nature :
`db_password`, le mot de passe du locataire du CT 200. Il n'est pas généré ici
— c'est `pg deploy --tenant` qui le produit ([§ 3](#3-la-base-locataire-du-ct-200)).

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
> PRA](PRA.md#cas-c--secrets-éphémères) —
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

## 9. Sauvegardes — ce que ce conteneur ne fait pas

**Ce conteneur ne sauvegarde rien lui-même.** C'est une décision, pas un
oubli : deux filets pour un même objet, c'est un filet que personne ne
surveille.

### Deux moitiés, deux propriétaires

```
la BASE      → cluster mutualisé du CT 200
               pg-backup.timer 02:30, puis pgbk-offsite 03:30 vers GCS
les DÉPÔTS   → vzdump du CT 400
               planification de sauvegarde du nœud
```

**Restaurer Forgejo demande LES DEUX**, pris à des instants proches. Une base
qui référence un dépôt absent du disque — ou l'inverse — donne une instance qui
démarre et se comporte n'importe comment.

### La base : rien à faire, elle est déjà couverte

`pg-backup` dumpe **toutes** les bases du cluster, `forgejo` comprise, dès que
le locataire existe. `pgbk-offsite` les emporte vers GCS. Aucune configuration
propre à Forgejo n'est nécessaire — c'est le principal bénéfice de la
mutualisation ([§ 3](#3-la-base-locataire-du-ct-200)).

Vérifier, **sur le nœud** :

```bash
pg status                    # les trois maillons de la sauvegarde
pg list                      # forgejo doit figurer dans les instantanés
pg show 20260821-023000      # le MANIFEST et les fichiers
```

### Les dépôts : à faire une fois

**Ajouter le CTID 400 à la sélection de sauvegarde du nœud**, et à ce qui part
vers GCS Nearline. Tant que ce n'est pas fait, **les dépôts n'ont aucune
copie** — la base seule ne restaure rien d'utile.

Ce qui est dans le `vzdump` du CT 400 :

| Contenu | Dedans ? |
|---|---|
| `/var/lib/forgejo` — dépôts, LFS, pièces jointes | oui |
| `/etc/forgejo` — `app.ini` **et les secrets** | oui |
| `/opt/forgejo` — le binaire épinglé | oui |
| la base | **non** — elle est dans le CT 200 |

La deuxième ligne mérite d'être remarquée : **le vzdump contient les secrets**.
C'est une bonne nouvelle en reprise ([§ 7](#7-les-secrets)), et une raison de
plus pour que le stockage de sauvegarde ne soit pas plus lisible que le
conteneur.

### Apparier un vzdump et un dump

C'est la seule difficulté de ce découpage. Les deux sauvegardes ne sont pas
prises au même instant — 02:30 pour la base, l'heure du vzdump pour les
dépôts — et rien ne les relie automatiquement.

En reprise, retenir **le dump le plus proche du vzdump, et de préférence
POSTÉRIEUR** : une base plus récente que les dépôts référence au pire quelques
dépôts absents, ce qui se voit et se corrige. Une base plus ancienne ignore des
dépôts présents sur le disque, ce qui ne se voit pas — ils sont simplement
invisibles dans l'interface, et on les croit perdus.

---

## 10. Restaurer

Trois cas, du plus courant au plus lourd. Le détail par scénario est dans le
[PRA](PRA.md) ; ce qui suit est la carte.

| Ce qui est perdu | Qui restaure | Où |
|---|---|---|
| La base seule | `pg restore forgejo` sur le CT 200 | [PRA § 1](PRA.md#1--la-base-est-perdue-ou-corrompue) |
| Le conteneur | `pct restore` du vzdump, puis `fj deploy` | [PRA § 3](PRA.md#3--le-conteneur-est-détruit) |
| Le nœud | tout, depuis GCS et les miroirs | [PRA § 4](PRA.md#4--le-nœud-est-perdu) |

### Après TOUTE restauration de la base

```bash
pg verify forgejo
```

**Les ACL ne sont ni dans le dump ni dans `globals.sql`.** Après une
restauration, `PUBLIC` retrouve `CONNECT` et l'isolation disparaît **en
silence** : la base remonte, tout a l'air normal. `pg verify` est ce qui le
dit, et il ne le dit que si on le joue.

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
La plupart sont automatisées — `fj deploy --status` et `fj status` les rendent ;
les deux dernières demandent une main.

| # | Ce qu'on vérifie | Comment |
|---|---|---|
| 1 | La version installée est bien une 15.0.x | `pct exec 400 -- /opt/forgejo/forgejo --version` |
| 2 | La connexion à la base fonctionne | `fj status` — le maillon « base (CT 200) » |
| 3 | `REVOKE CONNECT` toujours effectif | `pg verify forgejo` **sur le CT 200** |
| 4 | L'inscription publique est refusée | ouvrir `https://forgejo.lan.wittner.tech/user/sign_up` — doit refuser |
| 5 | Les trois `fsck` sont posés | `pct exec 400 -- git config --system --list \| grep fsck` |
| 6 | Le montage est en lecture seule vu du CT | `pct exec 400 -- grep forgejo-git /proc/mounts` — doit porter `ro` |
| 7 | Le mot de passe n'est lisible que par `git` | `pct exec 400 -- stat -c "%a %U:%G" /etc/forgejo/app.ini` — `640 root:git` |
| 8 | **Le CT remonte seul après redémarrage** | `pct reboot 400`, attendre, puis `fj status` |
| 9 | **Clone HTTPS et SSH depuis l'extérieur** | voir ci-dessous |

### 8. Le redémarrage

C'est la vérification qu'aucun contrôle automatique ne peut faire à votre
place : elle demande de couper pour de bon.

```bash
pct reboot 400
# attendre le retour, puis :
fj status
```

Les trois maillons doivent répondre, **sans aucune intervention**. Un service
qui ne remonte pas seul est un service qui ne remontera pas après une coupure
de courant — c'est-à-dire exactement quand on en a besoin.

C'est aussi le seul moment où l'ordre de démarrage se vérifie pour de bon : au
redémarrage du NŒUD, le CT 200 doit remonter avant le CT 400
([§ 3](#lordre-de-démarrage-au-boot-du-nœud)).

### 9. Le clone, dans les deux protocoles

Depuis une machine du LAN, **pas depuis le nœud** : le but est d'éprouver le
chemin complet, Traefik compris.

```bash
git clone https://forgejo.lan.wittner.tech/<org>/<dépôt>.git
git clone ssh://git@forgejo.lan.wittner.tech:2222/<org>/<dépôt>.git
```

Le second échoue tant que l'entryPoint `ssh` de Traefik n'a pas été pris en
compte — il est **statique**, donc il demande un redémarrage de Traefik
([§ 6](#6-routage-traefik)).
