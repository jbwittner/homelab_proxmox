# Runbook — VM Forgejo (`pve-eranikus`, VMID 300)

Le détail : création de la VM, conception, procédures rares, pièges rencontrés.
Ce qu'on tape au quotidien est dans [le README](../README.md) ; les mauvais
jours sont dans [doc/PRA.md](PRA.md).

**Les sections sont numérotées et stables.** Les scripts y renvoient par numéro
dans leurs messages d'erreur (`voir doc/RUNBOOK.md section 2`), et les unités
systemd les désignent par `Documentation=`. Déplacer ce document, c'est corriger
ces renvois dans le même geste.

## Sommaire

1. [Créer la VM](#1-créer-la-vm)
2. [Formater et étiqueter les trois disques de données](#2-formater-et-étiqueter-les-trois-disques-de-données)
3. [Déposer `init.sh` dans la VM, puis le lancer](#3-déposer-initsh-dans-la-vm-puis-le-lancer)
4. [Déployer la pile](#4-déployer-la-pile)
5. [Mettre à jour Forgejo](#5-mettre-à-jour-forgejo)
6. [Mettre à jour le système](#6-mettre-à-jour-le-système)
7. [La sauvegarde](#7-la-sauvegarde)
8. [La boucle assumée](#8-la-boucle-assumée)
9. [Pièges rencontrés](#9-pièges-rencontrés)

---

## 1. Créer la VM

**À la main, en `qm create`, et c'est une décision.** Terraform sur Proxmox est
un chantier séparé : il se fera pour tout le parc ou pour rien, pas pour une VM.

> **Règle** : si la VM est modifiée depuis l'interface web, **la commande
> ci-dessous est corrigée dans le même geste**. Une commande de création qui ne
> décrit plus la machine ne sert à rien le jour où il faut la recréer — et ce
> jour-là, c'est [le PRA scénario 3](PRA.md#3--nœud-perdu--sinistre) qui la lit.

### L'image

**Variante `genericcloud`, pas `generic`.** La `generic` embarque les pilotes
des matériels physiques ; sous KVM ils ne servent à rien et allongent l'image.

```bash
# Sur le nœud pve-eranikus
cd /var/lib/vz/template/iso
wget https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2
wget https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS
sha512sum --ignore-missing -c SHA512SUMS
# attendu : debian-13-genericcloud-amd64.qcow2: Réussi
```

La vérification n'est pas une formalité : c'est le système d'exploitation de la
source de vérité qui est en train d'être téléchargé, sur un lien HTTP dont
personne ne relit le contenu.

### La commande, complète

```bash
qm create 300 \
  --name forgejo \
  --description "Forgejo — source de vérité ArgoCD. Voir forgejo/README.md" \
  --cores 2 --sockets 1 --cpu host \
  --memory 4096 --balloon 0 \
  --net0 virtio,bridge=vmbr0 \
  --scsihw virtio-scsi-single \
  --ostype l26 \
  --serial0 socket --vga serial0 \
  --agent enabled=1 \
  --onboot 1 \
  --startup order=1

# --balloon 0 : mémoire fixe. Le ballon rend de la RAM à l'hyperviseur sous
# pression, et PostgreSQL réagit mal à un cache qui fond sous lui.
# --cpu host : conséquence assumée, la migration à chaud vers un nœud au
# processeur différent devient impossible. On restaure depuis vzdump.

# ── scsi0 — SYSTÈME, 20 Go, sauvegardé ───────────────────────────────────────
# Debian + Docker + /var/lib/docker (images, couches, journaux des conteneurs).
# Réinstallable de zéro : il ne porte AUCUNE donnée irremplaçable.
# Surveiller : chaque mise à jour d'image laisse l'ancienne couche derrière.
# `docker image prune` est dans le runbook § 5.
qm set 300 --scsi0 local-lvm:0,import-from=/var/lib/vz/template/iso/debian-13-genericcloud-amd64.qcow2,discard=on,ssd=1
qm disk resize 300 scsi0 20G

# ── scsi1 — /srv/forgejo, 40 Go, sauvegardé ──────────────────────────────────
# Les dépôts Git, le LFS, les pièces jointes ET la base PostgreSQL.
# UN SEUL disque pour les deux, délibérément : toute l'architecture repose sur
# « base et dépôts restaurés au même instant ». Les séparer rendrait la paire
# incohérente et casserait le --one-file-system de `fjbk backup`.
# Séparé du système pour que réinstaller Debian ne touche pas aux données, et
# pour que saturer la racine n'arrête pas PostgreSQL.
qm set 300 --scsi1 data:40,discard=on,ssd=1

# ── scsi2 — /srv/artifacts, 100 Go, SAUVEGARDÉ ───────────────────────────────
# Le registre de paquets Forgejo (images des applications).
# SAUVEGARDÉ, et c'est une décision : ArgoCD tire ses images d'ici, donc le
# registre est sur le chemin critique du démarrage du cluster. « Ça se
# reconstruit depuis le code » suppose une CI disponible — or la CI est DANS
# cette VM. Sans ce disque au restore, la reprise passe de vingt minutes à une
# demi-journée de pipelines rejoués.
# Forgejo ne purge pas les paquets seul : une politique de rétention est à
# écrire quand la CI existera. `fj-check.py` surveille le remplissage.
# Agrandissable à chaud : qm disk resize 300 scsi2 +50G puis resize2fs.
qm set 300 --scsi2 data:100,discard=on,ssd=1

# ── scsi3 — /srv/backup, 50 Go, backup=0 ─────────────────────────────────────
# Les 7 dernières paires produites par `fjbk backup` (dump PG + archive data),
# avant leur envoi vers GCS.
# backup=0 EST LE CŒUR DE LA DÉCISION, et il est ici et nulle part ailleurs :
# ces fichiers SONT déjà une sauvegarde, et la copie qui compte est chez GCS.
# Les embarquer dans chaque vzdump reviendrait à sauvegarder des sauvegardes —
# du volume pur, qui alourdit le seul artefact dont dépend le scénario « VM
# cassée » du PRA.
# Disque distinct pour que sept jours de rétention ne puissent JAMAIS remplir
# le disque des dépôts et arrêter PostgreSQL. C'est le mode de panne le plus
# banal d'un service qui sauvegarde à côté de ses données.
qm set 300 --scsi3 data:50,backup=0,discard=on,ssd=1

# ── cloud-init ───────────────────────────────────────────────────────────────
qm set 300 --ide2 local-lvm:cloudinit
qm set 300 --boot order=scsi0
qm set 300 --ciuser admin
qm set 300 --sshkeys /root/.ssh/authorized_keys
qm set 300 --ipconfig0 ip=192.168.1.56/24,gw=192.168.1.254
qm set 300 --nameserver 192.168.1.254

# --boot order=scsi0 : explicite. Avec quatre disques, s'en remettre à l'ordre
# par défaut se paie un jour au redémarrage.
# --ciupgrade 0 : cloud-init ne met rien à jour au premier démarrage. C'est
# init.sh qui le fait, une fois, de façon tracée.
qm set 300 --ciupgrade 0

qm start 300
```

`--sshkeys` désigne le fichier du nœud, et c'est une décision de reprise : voir
[Les clés SSH](#les-clés-ssh). **La suite est le
[§ 2](#2-formater-et-étiqueter-les-trois-disques-de-données)** — formater et
étiqueter les trois disques de données, à la main, une fois, en vérifiant deux
fois la cible.

### Pourquoi ces valeurs

| | |
|---|---|
| **2 vCPU, 4 Go** | Forgejo et PostgreSQL pour quelques utilisateurs. L'indexation d'un gros dépôt est le seul pic ; elle passe. |
| **`--balloon 0`** | Mémoire **fixe**. Le ballon rend de la RAM à l'hyperviseur dès qu'il en manque ailleurs, et PostgreSQL réagit mal à un cache qui fond sous lui. |
| **`--cpu host`** | Conséquence assumée : plus de migration à chaud vers un nœud au processeur différent. La reprise passe par le vzdump, pas par la migration. |
| **`--description`** | La seule phrase que lit quelqu'un qui ouvre l'interface de Proxmox sans connaître ce dépôt. Elle dit où est écrit le reste. |
| **`discard=on,ssd=1`** | `discard` fait redescendre les blocs libérés jusqu'au pool — sans lui, un fichier supprimé dans la VM continue d'occuper sa place en dessous. `ssd=1` évite que l'invité ordonne ses écritures comme sur un plateau. |
| **Quatre disques, pas un** | **Quatre cycles de vie.** Le système se réinstalle ; `/srv/forgejo` se sauvegarde en paire ; `/srv/artifacts` se reprend au vzdump ; `/srv/backup` sort de tout vzdump. Et surtout : remplir l'un ne peut pas arrêter les autres. |
| **`--onboot 1`, `--startup order=1`** | La source de vérité remonte la première après une coupure. Il n'y a plus d'ordre à respecter entre deux conteneurs : la base est dans la VM. |
| **`--agent enabled=1`** | `qm shutdown` obtient un arrêt propre plutôt qu'une coupure d'alimentation. Nécessaire aussi pour qu'un `qm snapshot` sache geler le système de fichiers. |
| **`--ciupgrade 0`** | cloud-init ne met pas à jour au premier démarrage : `init.sh` le fait, et il est le seul à décider quand. |

### Dimensionner les trois volumes

Ils ne se dimensionnent **pas indépendamment**, et ce n'est pas le plus gros qui
contraint :

| Volume | Taille | Ce qu'il porte |
|---|---|---|
| `/srv/forgejo` | **40 Go** | dépôts, LFS, pièces jointes — appelons-le **R** — plus la base PostgreSQL, qui pèse quelques centaines de Mo et ne bouge quasiment pas. Le registre n'y est pas. |
| `/srv/artifacts` | **100 Go** | le registre. **Rien ne le purge** : il ne fait que croître tant qu'une politique de rétention n'existe pas. |
| `/srv/backup` | **50 Go** | **7 paires, soit ≈ 7 × R comprimé** — et les objets git sont déjà comprimés, donc `tar czf` ne gagne pas grand-chose dessus. |

**C'est le disque des sauvegardes qui plafonne les dépôts, pas le leur.** À sept
jours de rétention, 50 Go tiennent de l'ordre de **7 Go de dépôts, pas 40**. Les
40 Go de `/srv/forgejo` ne sont donc pas remplissables sans toucher d'abord à la
rétention ou au disque des sauvegardes — c'est le genre de calcul qu'on préfère
faire ici qu'un matin à 3 h 10.

Ce qui a changé depuis le montage à deux disques : **saturer les sauvegardes
n'arrête plus PostgreSQL.** Elles sont sur leur propre volume, et le pire cas
est désormais `fjbk backup` qui refuse de démarrer sous **4 Gio libres**
(`FJBK_MIN_LIBRE_MO`) — une sauvegarde qui manque bruyamment, et rien de plus.

Trois leviers, dans l'ordre où on les tire :

1. **Agrandir le volume.** En ligne, sans rien arrêter — c'est le premier
   levier, et c'est pour ça que se tromper à la création coûte peu :

   ```bash
   qm disk resize 300 scsi3 +40G     # sur le NŒUD — ici, les sauvegardes

   # Dans la VM. `findmnt` résout le périphérique RÉEL derrière le point de
   # montage : aucune lettre n'est supposée, et il n'y a rien à vérifier deux
   # fois. Le même geste vaut pour scsi1 (/srv/forgejo) et scsi2
   # (/srv/artifacts) — seuls le slot et le chemin changent.
   sudo resize2fs "$(findmnt -no SOURCE /srv/backup)"
   df -h /srv/forgejo /srv/artifacts /srv/backup
   ```

   Pas de table de partitions à décaler : le système de fichiers occupe le
   disque entier ([§ 2](#2-formater-et-étiqueter-les-trois-disques-de-données)).

2. **Baisser la rétention locale** — `FJBK_RETENTION=3` dans
   `/etc/default/fjbk`. L'historique long vit dans GCS ; le local n'est là que
   pour restaurer vite, sans rapatrier.

3. **Écrire la politique de rétention du registre**, le jour où la CI existe.
   C'est le seul des trois volumes que personne ne purge.

### Le cas des artefacts

Forgejo sert ici de registre de paquets — **conteneurs OCI, Java, npm, Go** — et
ce registre **est sauvegardé**. C'est une décision, elle a été prise dans
l'autre sens auparavant, et il faut donc dire ce qui l'a retournée.

**« Ça se reconstruit depuis le code » suppose une CI disponible. Or la CI est
dans cette VM.** ArgoCD tire ses images du registre : celui-ci est sur le chemin
critique du démarrage du cluster. Sans ce disque au restore, la reprise ne coûte
pas quelques `docker push` — elle passe de **vingt minutes à une demi-journée de
pipelines rejoués**, à supposer que les pipelines puissent tourner, ce qui
suppose Forgejo debout. Un registre reconstructible en théorie et indisponible
en pratique n'est pas reconstructible.

**Mais il n'est pas sauvegardé n'importe comment**, et c'est là que tient
l'équilibre :

| | |
|---|---|
| `FORGEJO__storage_0X2E_packages__PATH: /packages` | le registre vit **hors de `/data`**, donc **hors de la paire nocturne**. Par défaut il serait sous `APP_DATA_PATH/packages`, et des dizaines de Go d'images immuables partiraient vers GCS chaque nuit, recomprimées à chaque fois. |
| Un **disque dédié** monté sur `/srv/artifacts` | remplir le registre ne peut pas remplir le volume des dépôts, donc ne peut pas arrêter PostgreSQL. |
| **Pas de `backup=0`** sur ce disque | **le vzdump le prend.** C'est lui, et lui seul, qui porte le registre. |

**La limite, écrite franchement : c'est le vzdump qui protège le registre, donc
il ne le protège que là où le vzdump protège.** Un vzdump qui reste sur le nœud
disparaît avec le nœud. Le [PRA scénario 2](PRA.md#2--vm-cassée) — VM cassée,
nœud sain — retrouve donc le registre intact ; le [scénario
3](PRA.md#3--nœud-perdu--sinistre) repart avec un registre **vide**, exactement
comme avant, tant que les vzdump ne sont pas répliqués hors du nœud. C'est un
[reste à faire](../README.md#reste-à-faire), et il n'est pas cosmétique : c'est
la moitié manquante de la décision ci-dessus.

**Personne ne purge ce volume.** Forgejo ne supprime pas seul les anciennes
versions de paquets. Une politique de rétention est à écrire le jour où la CI
publie pour de bon ; d'ici là, `fj-check.py` surveille le remplissage et le
disque s'agrandit à chaud.

#### Le mode de panne à connaître

Vérifié sur l'image 15.0.7 : au démarrage, Forgejo journalise

```
initPackages() [I] Initialising Packages storage with type: local
NewLocalStorage() [I] Creating new Local Storage at /packages
```

et **refuse de démarrer** s'il ne peut pas écrire là :

```
mustInit() [F] forgejo.org/modules/storage.Init failed: mkdir /packages: permission denied
```

C'est une bonne nouvelle : il tombe au lieu de dégrader. Mais il y a un cas où
il ne tombe **pas**, et c'est le seul mode de panne silencieux de ce montage —
**si un volume n'est pas monté**, son répertoire existe quand même, cette fois
sur le **disque système de 20 Go**, et tout s'y écrit sans que rien ne proteste :
les artefacts, ou les dépôts, ou les paires de sauvegarde, jusqu'à remplir la
racine.

D'où le contrôle `montages` de `fj-check.py`, qui vérifie que les trois en sont
bien :

```
  [KO ] montages     /srv/artifacts (registre) N'EST PAS MONTÉ — les monter avant toute écriture
```

### L'accès de secours

**L'image cloud ne définit aucun mot de passe.** Avec `--sshkeys` seul, on a un
accès SSH — et une invite de login série sur laquelle on ne peut rien taper. Le
jour où SSH ne monte pas (réseau mal configuré, `/etc/fstab` fautif qui bloque
le démarrage), **il n'y a aucun moyen d'entrer**.

Poser un mot de passe pour le compte `admin` est donc un geste de reprise, pas
un relâchement :

```bash
qm set 300 --cipassword "$(openssl rand -base64 18)"
# Proxmox le stocke haché dans la configuration de la VM. Le RANGER DANS SOPS
# immédiatement : il ne se relit pas depuis `qm config`.
```

À faire **avant le premier démarrage** : cloud-init n'applique le mot de passe
qu'à la première initialisation. Sur une VM déjà démarrée, il faut régénérer le
disque cloud-init et redémarrer — ce qui, sur une source de vérité, se décide.

L'accès reste par clé au quotidien : `PasswordAuthentication` de sshd n'est pas
touché, ce mot de passe ne sert **que** sur la console série.

### Les clés SSH

`--sshkeys /root/.ssh/authorized_keys` — **le fichier du nœud, pas une clé
publique dédiée.** C'est une décision de reprise avant d'être une commodité : le
jour où l'on recrée cette VM sur un nœud de repli, `authorized_keys` est là par
construction, puisqu'il a fallu s'y connecter en SSH pour taper la commande. Un
`/root/.ssh/forgejo-admin.pub` serait un fichier de plus à avoir pensé à
recopier, découvert absent au pire moment.

**Ça n'élargit l'accès à personne.** Qui a root sur le nœud peut déjà monter le
disque de la VM, ouvrir sa console avec `qm terminal` ou la détruire. Lui donner
en plus un accès SSH ne lui accorde rien qu'il n'ait déjà.

Deux réserves, à connaître :

- **Les préfixes d'options sont recopiés tels quels.** Une ligne
  `from="192.168.1.0/24",no-pty ssh-ed25519 …` du nœud arrive intacte dans la
  VM, où la restriction n'a pas le même sens. Relire le fichier avant :

  ```bash
  grep -c '^ssh-' /root/.ssh/authorized_keys      # combien de clés partent
  grep -v '^ssh-' /root/.ssh/authorized_keys      # celles qui portent des options
  ```

- **Ce n'est pas une synchronisation.** cloud-init pose ces clés au premier
  démarrage ; une clé ajoutée au nœud ensuite n'arrive pas toute seule dans la
  VM. Elle s'ajoute à la main, ou par un `qm set --sshkeys` suivi d'un
  redémarrage — ce qui, sur une source de vérité, se décide.

### L'adresse `192.168.1.56`

C'est **l'ancienne adresse du CT 200**, le cluster PostgreSQL mutualisé, retiré
en même temps que cette migration. Elle est libre. Conséquence à connaître :
toute règle de pare-feu, entrée DNS ou `.pgpass` qui désignait encore « la base
du homelab » sur cette adresse atteint désormais Forgejo. Il n'en restait aucune
au moment de la bascule, le CT 200 n'ayant eu qu'un locataire.

---

## 2. Formater et étiqueter les trois disques de données

> **DESTRUCTIF. À taper à la main, une seule fois, sur une VM neuve.**
> C'est le seul geste de tout ce montage qui puisse détruire les dépôts — ou,
> pire et plus vite, le système. `init.sh` ne le fait pas et ne le fera jamais :
> il vérifie que les **trois** étiquettes existent, et refuse de continuer si
> l'une manque.

**On est `admin`, connecté en SSH — donc tout ce qui suit passe par `sudo`.**
`mkfs.ext4` et `blkid` vivent dans `/usr/sbin`, que le `PATH` d'un utilisateur
ordinaire ne contient pas : sans `sudo`, ils répondent `command not found`, ce
qui accuse l'installation alors que le fautif est le chemin de recherche
([§ 9](#mkfsext4-command-not-found-et-le-paquet-est-installé--23-août-2026)).

Trois disques à formater, trois étiquettes, trois points de montage **frères** :

| Slot | Taille | Étiquette | Monté sur |
|---|---|---|---|
| `scsi1` | 40 Go | `srv` | `/srv/forgejo` |
| `scsi2` | 100 Go | `artifacts` | `/srv/artifacts` |
| `scsi3` | 50 Go | `backup` | `/srv/backup` |

`/srv` lui-même **reste sur le disque système**, et aucun des trois volumes
n'est sous un autre. C'est ce qui fait que remplir l'un ne peut pas empêcher les
autres d'écrire — et c'est aussi ce qui rend « non monté » un état possible et
silencieux, d'où le contrôle `montages` de `fj-check.py`.

### N'écrivez jamais `/dev/sdX` de mémoire

**L'ordre d'énumération SCSI ne suit pas les numéros de slot Proxmox.** Constaté
sur la VM 300 le 23 août 2026, sur le montage à trois disques d'alors :

```
sda    80G   ← scsi1, le disque de DONNÉES
sdb    20G   ← scsi0, LE DISQUE SYSTÈME : / et /boot/efi
sdc   200G   ← scsi2, le registre
```

`scsi0` avait pris `sdb`, pas `sda`. Un `mkfs.ext4 -L srv /dev/sdb` écrit de
mémoire, ou copié d'une procédure qui suppose l'ordre, **détruit le système**.

**Et à la recréation en quatre disques, la même machine a rangé ses lettres dans
l'ordre** : `scsi0` sur `sda`, `scsi1` sur `sdb`, et ainsi de suite. C'est le
pire résultat possible pour qui cherche une règle — deux créations de la même VM,
deux ordres différents. Une procédure écrite d'après la seconde marcherait, et
finirait un jour par viser le système. Le détail est en
[§ 9](#sda-nest-pas-scsi0--23-août-2026).

Les lettres ne sont donc pas une adresse. Les liens `by-id` de Proxmox, si :
chaque disque porte un numéro de série qui reprend son slot.

```bash
ls -l /dev/disk/by-id/ | grep 'drive-scsi'
# quatre liens : drive-scsi0 … drive-scsi3, chacun vers la lettre que le noyau
# lui a donnée CETTE FOIS-CI. C'est cette sortie qui fait foi, pas ce document :
# aucune correspondance lettre → slot n'est écrite ici, et c'est délibéré.
```

### Formater

Deux contrôles avant, et ils ne sont pas facultatifs : **la taille** et
**l'absence de partition**. Un disque neuf n'a ni l'une ni l'autre ; le disque
système porte des partitions et des points de montage.

```bash
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINTS
```

Voici ce que donne la VM 300 à quatre disques, **avant** le `mkfs` — relevé sur
la machine, `lsblk` nu :

```
admin@forgejo:/opt$ lsblk
NAME    MAJ:MIN RM  SIZE RO TYPE MOUNTPOINTS
sda       8:0    0   20G  0 disk
├─sda1    8:1    0 19.9G  0 part /
├─sda14   8:14   0    3M  0 part
└─sda15   8:15   0  124M  0 part /boot/efi
sdb       8:16   0   40G  0 disk
sdc       8:32   0  100G  0 disk
sdd       8:48   0   50G  0 disk
sr0      11:0    1    4M  1 rom
```

Ce qui se lit là-dedans — et ce qui ne s'y lit pas :

| | |
|---|---|
| `sda` porte `/` et `/boot/efi` | **c'est le système, on n'y touche pas.** Il se reconnaît à ses partitions et à ses points de montage, jamais à sa lettre. Les `sda14`/`sda15` de 3 et 124 Mo sont les partitions d'amorçage de l'image cloud. |
| `sdb`, `sdc`, `sdd` sont **nus** | ni partition, ni système de fichiers : c'est exactement l'état attendu avant le `mkfs`. Si l'un d'eux portait quoi que ce soit, **s'arrêter** et comprendre pourquoi. |
| 40, 100 et 50 Go | **trois tailles distinctes, et c'est ce qui sauve.** Chaque disque de données s'identifie à sa seule taille. Deux volumes de même taille auraient rendu `lsblk` muet sur la question, et `by-id` serait alors le seul recours. |
| `sr0`, 4 Mo, `rom` | le lecteur cloud-init (`ide2`). **S'il manque, cloud-init n'a aucune source de données** — pas d'`admin`, pas de clé, pas d'IP : voir [§ 9](#la-console-série-qui-ne-dit-rien--23-août-2026). |
| ce qui **ne** s'y lit **pas** | **quel slot Proxmox est derrière quelle lettre.** `lsblk` ne le dit pas, et cette sortie ne vaut que pour ce démarrage-là. |

La forme `-o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINTS` ci-dessus ajoute les deux
colonnes qui manquent ici — système de fichiers et étiquette. Elles sont vides
avant le `mkfs` ; c'est **après** qu'on les relit.

Puis, en nommant les cibles par leur slot et non par leur lettre :

```bash
SRV=/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_drive-scsi1     # 40 Go
ART=/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_drive-scsi2     # 100 Go
BKP=/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_drive-scsi3     # 50 Go

# DERNIER CONTRÔLE avant d'écrire : les tailles doivent être celles ci-dessus,
# et aucun des trois ne doit porter la moindre partition.
lsblk "$SRV" "$ART" "$BKP"

sudo mkfs.ext4 -L srv       "$SRV"
sudo mkfs.ext4 -L artifacts "$ART"
sudo mkfs.ext4 -L backup    "$BKP"
```

Les variables sont développées par **votre** shell avant que `sudo` ne
s'exécute : les définir en `admin` puis préfixer par `sudo` fonctionne, il n'y a
rien à réexporter.

#### Relire ce que `mkfs` vient de dire

Sa sortie porte deux confirmations qu'on laisse défiler à tort.

**Le nombre de blocs redonne la taille**, et c'est la façon la plus directe de
constater qu'on n'a pas formaté deux fois le même disque :

```
Creating filesystem with 10485760 4k blocks and 2621440 inodes     ← srv
Creating filesystem with 26214400 4k blocks and 6553600 inodes     ← artifacts
Creating filesystem with 13107200 4k blocks and 3276800 inodes     ← backup
```

| Étiquette | Blocs annoncés | × 4 ko | Attendu |
|---|---|---|---|
| `srv` | 10 485 760 | **40 Gio** | 40 |
| `artifacts` | 26 214 400 | **100 Gio** | 100 |
| `backup` | 13 107 200 | **50 Gio** | 50 |

Trois nombres différents pour trois disques différents. **Deux nombres
identiques voudraient dire qu'une variable pointait deux fois le même disque** —
et le second `mkfs` aurait effacé le premier sans le moindre avertissement,
puisque du point de vue de `mkfs` il n'y a rien d'anormal à formater un volume
qui vient de l'être.

**`Discarding device blocks: done`** est la seconde : le `discard=on` posé à la
création de la VM ([§ 1](#1-créer-la-vm)) a bien pris, et les blocs libérés
redescendent jusqu'au pool. Sur un disque qui ne le supporterait pas, cette
ligne n'apparaîtrait pas.

Si `by-id` n'expose pas ces liens sur votre machine, retomber sur les lettres —
mais **relues dans `lsblk` à l'instant**, jamais reprises d'un document.

### Monter, par étiquette et jamais par lettre

`init.sh` écrit ces trois lignes dans `/etc/fstab` ; elles sont ici pour être
relues, et pour le jour où il faut les remettre à la main :

```
LABEL=srv       /srv/forgejo   ext4 defaults 0 2
LABEL=artifacts /srv/artifacts ext4 defaults 0 2
LABEL=backup    /srv/backup    ext4 defaults 0 2
```

**L'étiquette, pas le nom de périphérique** — c'est la même leçon qu'au-dessus :
entre deux démarrages, la lettre peut changer, l'étiquette non.

### Vérifier

> **`lsblk` tout nu affiche exactement la même chose qu'avant le `mkfs`** — même
> lettres, mêmes tailles, rien de plus. Ce n'est pas que le formatage a échoué :
> c'est que les colonnes `FSTYPE` et `LABEL` ne sont pas affichées par défaut.
> Il faut les demander, sinon on cherche une confirmation là où elle ne peut pas
> être.

```bash
# -c /dev/null : sonder les disques SANS le cache /run/blkid/blkid.tab, qui est
# en retard sur un mkfs tout juste fait. C'est la forme qu'init.sh utilise, pour
# ne pas refuser de démarrer sur un volume parfaitement formaté.
sudo blkid -c /dev/null -L srv
sudo blkid -c /dev/null -L artifacts
sudo blkid -c /dev/null -L backup
# lsblk est dans /usr/bin, lui : pas de sudo nécessaire.
lsblk -o NAME,SIZE,FSTYPE,LABEL
```

Chaque `blkid` ne répond qu'un chemin, et c'est tout ce qu'on lui demande : le
disque qui porte l'étiquette. C'est **court au point d'avoir l'air d'un échec**,
ce n'en est pas un.

```
admin@forgejo:/opt$ sudo blkid -c /dev/null -L srv
/dev/sdb
admin@forgejo:/opt$ sudo blkid -c /dev/null -L artifacts
/dev/sdc
admin@forgejo:/opt$ sudo blkid -c /dev/null -L backup
/dev/sdd
admin@forgejo:/opt$ lsblk -o NAME,SIZE,FSTYPE,LABEL
NAME     SIZE FSTYPE  LABEL
sda       20G
├─sda1  19.9G ext4
├─sda14    3M
└─sda15  124M vfat
sdb       40G ext4    srv
sdc      100G ext4    artifacts
sdd       50G ext4    backup
sr0        4M iso9660 cidata
```

C'est la même machine qu'au [§ Formater](#formater), après. Ce qu'il faut y
lire, dans l'ordre :

| | |
|---|---|
| **40 G ↔ `srv`, 100 G ↔ `artifacts`, 50 G ↔ `backup`** | **la taille EN FACE de l'étiquette**, et pas seulement le fait que les trois répondent. C'est le seul contrôle qui attrape une interversion, et une interversion ne se signale jamais d'elle-même. |
| les trois `blkid` donnent **trois chemins distincts** | `sdb`, `sdc`, `sdd`. Deux fois le même chemin voudrait dire deux étiquettes sur un seul disque — donc un `mkfs` qui a écrasé l'autre. |
| `sda1` et `sda15` n'ont **aucune étiquette** | les partitions du système n'en portent pas. `blkid -L srv` ne peut donc structurellement pas les désigner, et `init.sh` ne peut pas monter la racine par erreur. |
| `sr0`, `iso9660`, **`cidata`** | **le disque cloud-init**, vu de l'intérieur. `cidata` est le nom que cherche la source de données NoCloud : le voir ici prouve que `ide2` est bien attaché, sans aller interroger le nœud — voir [§ 9](#la-console-série-qui-ne-dit-rien--23-août-2026). |

**Intervertir deux étiquettes est le second pire scénario de cette section** —
après avoir visé le système. Aucun des deux échanges ne se signale tout seul :

- `srv` ↔ `backup` : les dépôts partiraient sur le disque `backup=0`, donc
  **hors de tout vzdump**, et les sauvegardes sur le disque sauvegardé. Rien ne
  le dirait avant le jour où l'on chercherait une sauvegarde.
- `srv` ↔ `artifacts` : les deux sont bien repris par le vzdump, mais 40 et
  100 Go ne sont pas interchangeables, et la paire nocturne archiverait le
  registre nuit après nuit — précisément ce que tout le montage évite.

C'est pour cela que la vérification porte sur **la taille en face de
l'étiquette**, et pas seulement sur l'existence des trois.

Pas de partition : le système de fichiers occupe le disque entier. Un disque
virtuel s'agrandit par `qm disk resize` puis `resize2fs`, sans table de
partitions à décaler.

**La suite est le [§ 3](#3-déposer-initsh-dans-la-vm-puis-le-lancer)** — et elle
commence par **revenir sur le poste**.

## 3. Déposer `init.sh` dans la VM, puis le lancer

**Le fichier arrive par `scp`, pas par `git`** — et il n'y a pas d'autre choix :
l'image `genericcloud` n'a pas `git`, et c'est justement `init.sh` qui
l'installe. Cloner d'abord serait impossible. C'est aussi le seul moment où ce
fichier voyage seul, et c'est tout l'intérêt d'un script d'un seul fichier sans
dépendance : il se copie et il tourne.

> Le [§ 2](#2-formater-et-étiqueter-les-trois-disques-de-données) se joue
> **dans** la VM ; celui-ci commence **sur le poste**. C'est le seul
> aller-retour de tout le montage, et il vaut mieux le voir venir que le
> découvrir en cherchant un `git` qui n'est pas là.

### Le déposer

```bash
# SUR LE POSTE, à la racine du clone local de ce dépôt
cd ~/workspace/homelab_proxmox
scp pve-eranikus/forgejo/scripts/init.sh admin@192.168.1.56:/tmp/
```

Si le poste n'a pas ce dépôt sous la main — poste neuf, dépannage — il l'obtient
en une commande. **C'est le poste qui a `git`, pas la VM**, et c'est tout le
propos :

```bash
git clone https://github.com/<org>/homelab_proxmox.git
cd homelab_proxmox
```

Le `scp` s'authentifie avec la clé que cloud-init a posée au premier démarrage
(`--sshkeys`, [§ 1](#1-créer-la-vm)) : il n'y a rien à configurer. **S'il demande
un mot de passe, c'est que cloud-init n'a pas fait son travail** — la VM démarre
alors très bien et n'a ni `admin`, ni clé, ni IP fixe :
[§ 9](#la-console-série-qui-ne-dit-rien--23-août-2026).

### Le lancer

```bash
# Dans la VM
ssh admin@192.168.1.56
ls -l /tmp/init.sh          # arrivé entier ? ~7 ko
sudo bash /tmp/init.sh
```

`sudo bash /tmp/init.sh`, et non `./init.sh` : `scp` ne transporte pas le bit
d'exécution, et un `chmod +x` de plus est une étape de plus à oublier.

Ensuite seulement le dépôt se clone ([§ 4](#4-déployer-la-pile)), et les
exécutions suivantes utilisent la copie versionnée :

```bash
sudo /opt/homelab/pve-eranikus/forgejo/scripts/init.sh
# → refusera, témoin posé. C'est le comportement voulu.
```

Il pose : mise à jour du système, montage des **trois** volumes de données par
étiquette — `/srv/forgejo`, `/srv/artifacts`, `/srv/backup` —, `git` (absent de
l'image, et nécessaire au clone du § 4), le dépôt
Docker CE officiel (la clé dans `/etc/apt/keyrings`) puis `docker-ce
docker-ce-cli containerd.io docker-compose-plugin`, `rclone`, la rotation des
journaux Docker,
`admin` dans le groupe `docker`, les mises à jour automatiques restreintes à la
sécurité, **la paire de clés SSH de déploiement de la machine**, le fuseau
horaire, `qemu-guest-agent`.

Il termine en affichant la clé publique — c'est la passe de main vers le § 4, et
la seule chose qu'un humain doit recopier ailleurs :

```
14:22:07 [INIT ] CLÉ PUBLIQUE À DÉPOSER DANS FORGEJO, en LECTURE SEULE :
         ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA… deploy-vm300-forgejo
```

La clé **privée** ne quitte jamais la VM, et le dépôt n'en sait rien.

### Le témoin

`init.sh` écrit `/var/lib/homelab/init.done` — la date ISO de fin — **à la
toute fin**, et refuse de repartir si le fichier existe :

```
14:22:07 [ERROR] déjà provisionné le 2026-08-22T14:20:11+02:00 — voir doc/RUNBOOK.md section 3
```

Et s'il manque une étiquette, il refuse **avant** d'installer quoi que ce soit —
les trois sont vérifiées, pas seulement la première :

```
14:19:02 [ERROR] aucun volume étiqueté « backup » — le formater d'abord, voir doc/RUNBOOK.md section 2
```

Écrit à la fin, et non au début : un script interrompu au milieu (réseau coupé,
`apt` qui échoue) doit pouvoir être relancé tel quel. Ce n'est **pas** un script
rejouable au sens du convergent d'avant — ce dépôt n'a plus de moteur — c'est un
script qui pose un système une fois et refuse ensuite.

Pour le rejouer volontairement, sur une VM dont on sait ce qu'on fait :

```bash
sudo rm /var/lib/homelab/init.done
```

---

## 4. Déployer la pile

Deux choses arrivent dans la VM, **par deux chemins différents, et ce n'est pas
un détail** : le dépôt par `git`, le `.env` par `scp`.

### Le dépôt, par clé de déploiement en lecture seule

**La clé a déjà été générée par `init.sh`**, dans la VM, au nom de `admin` — et
elle n'en sortira pas. Une clé recopiée d'une machine à l'autre ne se révoque
plus machine par machine, or c'est tout ce qu'on demande à une clé de
déploiement. Il reste à en déposer la **partie publique** dans Forgejo.

```bash
# Dans la VM — la relire, si la sortie d'init.sh est déjà passée
cat ~/.ssh/id_ed25519.pub
# ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA… deploy-vm300-forgejo
```

Puis, dans l'interface : le dépôt → **Paramètres** → **Clés de déploiement** →
*Ajouter une clé*. Coller la ligne, et **ne pas cocher « Accès en écriture »**.
Cette VM n'a aucune raison de pouvoir écrire dans le dépôt qu'elle sert : c'est
la lecture seule qui protège ici, et rien d'autre.

Si `init.sh` n'a pas tourné — une machine de dépannage, un essai — la même clé
se produit à la main :

```bash
ssh-keygen -t ed25519 -N '' -C "deploy-vm300-forgejo" -f ~/.ssh/id_ed25519
```

`-N ''`, donc **sans phrase de passe**, et c'est délibéré : un `git pull` non
interactif ne peut pas en saisir une, et une phrase de passe rangée à côté de
la clé qu'elle protège ne protège rien.

### Le clone passe par le port 2222, jamais par 22

```bash
# Dans la VM
sudo mkdir -p /opt/homelab
sudo chown admin:admin /opt/homelab

# La clé d'hôte AVANT le clone : sans elle, la première connexion s'arrête sur
# une question à laquelle personne ne répondra dans un script.
ssh-keyscan -p 2222 forgejo.wittner.tech >> ~/.ssh/known_hosts

# Éprouver la clé AVANT de cloner : Forgejo salue et refuse le shell, ce qui
# est exactement ce qu'on veut voir. Un « Permission denied (publickey) » ici
# dit que la clé publique n'est pas déposée, ou pas sur le bon dépôt.
ssh -T -p 2222 git@forgejo.wittner.tech

git clone ssh://git@forgejo.wittner.tech:2222/homelab/homelab_proxmox.git /opt/homelab
```

> **`git clone git@forgejo.wittner.tech:homelab/…` ne marche pas**, et l'erreur
> ne se lit pas dans son message. La forme courte `hôte:chemin` vise le **port
> 22** — où répond le sshd d'administration de la VM, pas Forgejo. Le serveur
> SSH de Forgejo est **interne au conteneur** (`START_SSH_SERVER`), publié en
> 2222 par la VM et routé par Traefik : il faut la forme longue
> `ssh://…:2222/…`. C'est la même adresse que celle des clones utilisateurs,
> et c'est normal — il n'y a qu'un seul chemin SSH vers Forgejo.

> `ssh-keyscan` fait confiance à ce qu'il trouve, une fois, sans rien vérifier.
> Sur ce LAN c'est acceptable ; sur un lien qu'on ne maîtrise pas, ça ne l'est
> pas. Le contrôle honnête est de comparer l'empreinte obtenue à celle que
> Forgejo affiche, avant de la garder.

Pendant la transition, le clone se fait depuis le miroir GitHub — voir
[§ 8](#8-la-boucle-assumée). **C'est la même clé publique** qui s'y dépose : ce
qui change est l'endroit où elle est déclarée, pas la clé, qui reste née dans
cette machine et n'en bouge pas.

### Le `.env`, par scp depuis le poste

```bash
# Sur le POSTE de travail, jamais dans la VM
sops -d forgejo.env.sops > /tmp/.env
scp /tmp/.env admin@192.168.1.56:/opt/homelab/pve-eranikus/forgejo/.env
shred -u /tmp/.env
```

**Il n'arrive jamais par `git pull`.** La clé age qui le déchiffre reste sur le
poste : la déposer dans la VM reviendrait à ranger la clé sous le paillasson de
la porte qu'elle ferme. `env.example` porte les clés attendues, sans valeur.

### Générer les quatre secrets, la première fois

```bash
for cle in SECRET_KEY INTERNAL_TOKEN JWT_SECRET JWT_SECRET; do
  docker run --rm codeberg.org/forgejo/forgejo:15.0.7 forgejo generate secret "$cle"
done
```

Les deux derniers alimentent `FORGEJO_OAUTH2_JWT_SECRET` et
`FORGEJO_LFS_JWT_SECRET` : **deux valeurs distinctes**, ils ne partagent pas
leur secret.

Le mot de passe de la base — s'en tenir à de l'alphanumérique, voir
[§ 9](#9-pièges-rencontrés) :

```bash
openssl rand -hex 24
```

**Pourquoi les quatre, et pas deux.** Forgejo génère lui-même celui qui lui
manque, et **réécrit sa configuration au passage**. Les pré-déposer tous les
quatre est ce qui rend une configuration entièrement déclarative tenable : sans
ça, `compose.yaml` décrit un état que le service quitte au premier démarrage.

### Démarrer

```bash
cd /opt/homelab/pve-eranikus/forgejo
docker compose pull
docker compose up -d
./scripts/fj-check.py
```

### Armer la sauvegarde

```bash
sudo install -m 0644 scripts/fjbk.service scripts/fjbk.timer /etc/systemd/system/
sudo systemctl daemon-reload
# Le bucket AVANT le timer : un timer armé sur une configuration incomplète
# échoue toutes les nuits à 3 h et n'aide personne.
sudo tee /etc/default/fjbk >/dev/null <<'EOF'
FJBK_BUCKET=<le-bucket-dédié>
FJBK_PREFIX=forgejo
FJBK_RETENTION=7
EOF
sudo systemctl enable --now fjbk.timer
sudo systemctl list-timers fjbk.timer
```

---

## 5. Mettre à jour Forgejo

**Jamais automatique.** Ni par timer, ni par `latest`. Passer d'une version à
une autre est une décision qui se prend en lisant les notes de publication et
qui se commite — Forgejo migre son schéma de base au démarrage, et cette
migration ne se défait pas.

L'ordre compte, et **le snapshot est un geste hôte** : il ne peut pas être dans
un script qui tourne dans la VM.

```bash
# 1. Sur le NŒUD — le filet, avant tout le reste
qm snapshot 300 avant-forgejo-15-0-8

# 2. Dans le dépôt, sur le POSTE — l'épinglage est une ligne de git log
#    forgejo/compose.yaml : image: codeberg.org/forgejo/forgejo:15.0.8
git commit -am "Forgejo 15.0.7 → 15.0.8 : <ce que disent les notes>"
git push

# 3. Dans la VM
cd /opt/homelab && git pull
cd forgejo && docker compose pull && docker compose up -d

# 4. Vérifier — la migration de schéma se voit dans le journal
docker compose logs -f forgejo
./scripts/fj-check.py

# 5. Dans la VM — l'ancienne image reste sur le disque système
docker image prune -f
docker image ls | grep forgejo

# 6. Sur le NŒUD, une fois sûr — un snapshot oublié grossit en silence
qm delsnapshot 300 avant-forgejo-15-0-8
```

**`docker image prune` n'est pas une coquetterie de rangement.** Le disque
système fait 20 Go et chaque mise à jour y laisse la couche précédente ; c'est
la seule chose qui grossit toute seule sur ce volume. `-f` évite la question
interactive, et sans `-a` la commande ne touche qu'aux images sans conteneur —
l'image épinglée en service n'est jamais candidate. Le faire **après** avoir
vérifié que la nouvelle version tourne : tant que le snapshot est là, revenir en
arrière ne dépend pas de l'image locale, mais autant ne pas la jeter avant.

Si ça se passe mal : `qm rollback 300 avant-forgejo-15-0-8` depuis le nœud.
C'est **le seul moyen de revenir en arrière sur une migration de schéma** —
remettre l'ancienne image sur une base déjà migrée ne marche pas.

La branche **15.0 est une LTS, supportée jusqu'au 15 juillet 2027**. Passer en
16 ou 17 est un autre exercice, à préparer avec un `fjbk backup` frais et un
snapshot.

---

## 6. Mettre à jour le système

```bash
sudo /opt/homelab/pve-eranikus/forgejo/scripts/sys-update.sh
```

`dist-upgrade` et non `upgrade` : sur Debian stable, `upgrade` **retient
silencieusement** tout paquet dont la mise à jour demande d'en installer un
nouveau. C'est exactement ce qui arrive quand Docker CE découpe un paquet — le
système se croit à jour et ne l'est pas.

**Le script ne redémarre jamais.** Il signale, et sort :

```
09:14:02 [SYS  ] REDÉMARRAGE REQUIS — il n'est PAS fait ici.
09:14:02 [SYS  ] paquets qui le demandent :
         linux-image-amd64
```

Le redémarrage d'une source de vérité se décide, avec un snapshot pris avant :

```bash
qm snapshot 300 avant-reboot-20260822     # sur le NŒUD
sudo systemctl reboot                      # dans la VM
```

Les mises à jour **de sécurité** passent seules, par `unattended-upgrades`
restreint à l'origine `Debian-Security` et avec `Automatic-Reboot "false"`. Tout
le reste attend qu'on le décide.

---

## 7. La sauvegarde

### Ce que fait `fjbk backup`

1. `docker compose stop forgejo` — **la base continue de tourner**
2. `pg_dump -Fc --no-owner --no-acl` via le conteneur `db` → `db-<horodatage>.dump`
3. `tar czf --one-file-system` de `/srv/forgejo/data` →
   `data-<horodatage>.tar.gz`, en excluant `data/gitea/log` et
   `data/gitea/indexers` (Forgejo les reconstruit). `--one-file-system` fait que
   l'archive **ne quitte jamais le volume des dépôts** : « base et dépôts au
   même instant » devient vrai par construction, et non par convention.
   **Le registre d'artefacts n'y est pas** : il vit hors de `/data`, sur son
   disque, et il est sauvegardé **par le vzdump du nœud**, pas par la paire —
   voir [Le cas des artefacts](#le-cas-des-artefacts)
4. **redémarrage de Forgejo, dans un `finally`, quoi qu'il arrive**
5. `rclone copy` de la paire vers GCS
6. purge locale au-delà de 7 jours, dans `/srv/backup` et **là seulement** —
   le distant relève du cycle de vie du bucket
7. appel de `fj-check.py`, dont le verdict devient le code de sortie

### Pourquoi Forgejo est arrêté pendant l'archivage

Forgejo écrit dans la base **et** dans les dépôts, souvent dans la même
transaction logique : un push crée des objets sur disque et une ligne en base.
Le laisser tourner produirait une archive de dépôts prise en cours d'écriture et
un dump qui ne lui correspond pas — deux moitiés qui ne se recollent pas.

La base, elle, reste debout : c'est elle qui sert le `pg_dump`, et plus personne
ne lui écrit une fois Forgejo arrêté.

Le redémarrage est dans un `finally`. **Un job de sauvegarde ne laisse jamais le
service à terre** — c'est la raison pour laquelle `fj-check.py` est appelé juste
après, et pourquoi son verdict remonte dans le code de sortie.

### Pourquoi une paire, et pas deux sauvegardes séparées

C'est la raison d'être de toute cette architecture. Avant, la base était
sauvegardée par le CT 200 et les dépôts par un `vzdump` du CT 400 : **deux
filets, deux propriétaires, deux horaires**. Restaurer demandait de trouver un
dump et un vzdump qui se recouvrent, puis de les rejouer dans le bon ordre sur
deux machines. Le seul document qui décrivait comment les apparier était le
runbook, et rien ne vérifiait que la paire existait.

Ici, une paire porte le même horodatage parce qu'elle est prise dans la même
seconde, sur la même machine, service arrêté. `fjbk list` signale une paire
incomplète ; `fj-check.py` refuse une paire de plus de 48 h.

### La doctrine hors-site

Reprise **intégralement** de l'ancien `pgtool/offsite.py`, et elle n'est pas
négociable :

- **`copy`, jamais `sync`.** `sync` réplique les suppressions : un volume
  démonté ou un répertoire vidé, et la copie distante disparaît avec
  l'originale.
- **`--ignore-existing`**, parce que le compte de service est `objectViewer` +
  `objectCreator` : il liste, lit et crée, **il n'écrase ni ne supprime**. Une
  VM compromise ne peut pas détruire l'historique.
- **La rétention distante est une règle de cycle de vie du bucket**, jamais
  l'affaire du code. `fjbk` purge le local, et rien d'autre.
- Le compte de service est un compte **dédié**, sa clé vit dans
  `/root/.config/rclone/forgejo-backups.json`, hors dépôt par construction.

### Le contrat de codes de retour

| Code | Ce que ça veut dire | Quoi faire |
|---|---|---|
| `0` | tout va bien | rien |
| `1` | environnement inutilisable — docker, rclone, clé, bucket, chemins | corriger la machine ; c'est aussi le code d'une faute de frappe sur la ligne de commande |
| `2` | une opération a échoué | lire le journal, rejouer |
| `3` | la pile demande une intervention — `fj-check.py` est rouge | [doc/PRA.md](PRA.md) |
| `130` | interrompu par signal | vérifier que Forgejo est bien remonté |

**Tout code non nul marque l'unité systemd en échec, et c'est voulu.** Ne pas
ajouter de `SuccessExitStatus` pour lisser le 3 : c'est précisément celui qui
dit que Forgejo n'est peut-être pas remonté.

### Éprouver une sauvegarde

```bash
sudo /opt/homelab/pve-eranikus/forgejo/scripts/fjbk list
sudo /opt/homelab/pve-eranikus/forgejo/scripts/fjbk verify 20260822-030412
```

`verify` lit le dump par `pg_restore --list` et l'archive par `tar -t`, **sans
rien restaurer**. Une sauvegarde jamais éprouvée est une hypothèse. La vraie
épreuve reste [le PRA joué sur une VM jetable](PRA-exercice.md).

---

## 8. La boucle assumée

**Ce dépôt sera cloné dans la VM depuis Forgejo lui-même.** Forgejo héberge la
configuration de Forgejo. C'est une circularité, elle est assumée — et c'est
pour ça qu'elle est écrite ici : **une boucle assumée qui n'est écrite nulle
part redevient une boucle subie.**

### Pourquoi c'est acceptable

La boucle ne gêne que dans un seul cas : Forgejo est mort **et** on a besoin du
dépôt. Or dans ce cas précis, **on ne fait pas `git pull`** — on joue le PRA, et
[le scénario 4](PRA.md#4--forgejo-est-mort-et-jai-besoin-du-dépôt) part
délibérément d'ailleurs.

Le reste du temps, la boucle ne coûte rien : mettre à jour la pile suppose que
Forgejo tourne, ce qui est précisément le cas où il peut servir son propre
dépôt.

### Les deux sorties

Elles ne sont pas des raffinements. Ce sont les deux choses qui font que la
boucle n'est pas un piège :

1. **Le clone local sur le poste de travail.** Un clone git complet est une
   copie complète de l'historique. C'est de là que part le scénario 4 du PRA :
   `scp` du `compose.yaml` et du `.env`, `docker compose up -d`, sans jamais
   interroger Forgejo.

2. **Le push mirror vers GitHub**, qui **reste actif en permanence**, y compris
   après la migration du dépôt dans Forgejo. Sortant uniquement : Forgejo pousse
   vers GitHub, GitHub ne sait rien de Forgejo et ne peut rien lui demander —
   aucune dépendance entrante n'est créée.

> Le miroir n'est **pas** une sauvegarde : il ne porte ni les tickets, ni les
> demandes d'ajout, ni les comptes, ni les clés. Il porte les objets git, ce qui
> est exactement ce dont ArgoCD — et une reprise — ont besoin.

### Les clés de déploiement pendant la transition

Le clone se fait d'abord depuis GitHub, puis depuis Forgejo. **Les deux clés
existent donc en même temps pendant la bascule.**

**Celle qui ne sert plus est supprimée, dans le même geste que la bascule.** Une
clé de déploiement oubliée sur un miroir se retrouve trois ans plus tard, encore
valide, sur un dépôt dont plus personne ne surveille les accès.

- [ ] clé publique de la VM (`~admin/.ssh/id_ed25519.pub`, produite par
      `init.sh`) déclarée en **lecture seule** sur le dépôt GitHub, clone
      initial fait
- [ ] dépôt migré dans Forgejo, miroir push configuré et vérifié
- [ ] **la même** clé publique déclarée en lecture seule sur le dépôt Forgejo,
      puis `git remote set-url origin ssh://git@forgejo.wittner.tech:2222/homelab/homelab_proxmox.git`
      dans la VM
- [ ] **déclaration GitHub supprimée** — celle-ci, pas le miroir. Une clé de
      déploiement oubliée se retrouve trois ans plus tard, encore valide.

---

## 9. Pièges rencontrés

Chacun avec sa date et le message exact : c'est ce qui le rend reconnaissable la
fois suivante.

### Le jeton de miroir est chiffré par `SECRET_KEY` — 21 août 2026

Le jeton d'accès GitHub d'un miroir push est stocké **chiffré par `SECRET_KEY`**
dans la base. Une instance reconstruite avec un autre `SECRET_KEY` démarre
parfaitement, sert les dépôts, et **voit ses miroirs échouer en silence**.

C'est le seul dégât vraiment irréversible de ce montage : aucune restauration de
base ne le répare, parce qu'il ne s'agit pas de données perdues mais de données
illisibles. D'où les quatre secrets rangés chiffrés hors de la VM
([§ 4](#4-déployer-la-pile)).

### L'entryPoint `ssh` de Traefik est statique — 21 août 2026

Le clone SSH échoue tant que Traefik n'a pas été **redémarré** : l'entryPoint
`ssh` est déclaré dans `traefik.yaml`, qui est la configuration *statique*. Le
répertoire `dynamic/` est surveillé, pas celui-là.

```
ssh: connect to host forgejo.wittner.tech port 2222: Connection refused
```

### `passHostHeader: true` est obligatoire — repris de l'ancien montage

Sans lui, une connexion **réussie** renvoie le navigateur vers
`http://192.168.1.56:3000/` : Forgejo construit ses URL de redirection à partir
de l'en-tête `Host`, et le navigateur sort du TLS sans que rien n'ait l'air
cassé. Le commentaire est dans
[`pve-ysera/traefik/dynamic/forgejo.yaml`](../../../pve-ysera/traefik/dynamic/forgejo.yaml),
il y reste.

### Les deux-points dans un mot de passe — 21 août 2026

L'ancienne sonde écrivait une ligne `.pgpass`, où les deux-points séparent les
champs. Un mot de passe qui en contenait produisait
`password authentication failed` — c'est-à-dire **exactement le message d'un
mauvais mot de passe**, alors que le secret était juste.

Le `.pgpass` a disparu avec le cluster mutualisé, mais la leçon vaut pour le
`.env` : **s'en tenir à de l'alphanumérique**. `openssl rand -hex 24` ne produit
que ça, et évite le problème partout à la fois — fichier d'environnement,
gestionnaire de secrets, ligne de commande.

### Un code de retour qui ment sur la cause — 21 août 2026, revu le 22

Constaté sur l'ancien outil : `pg offsite --foo` sortait en **2**, code qui
voulait dire « transfert en échec ». Une faute de frappe se lisait donc comme
une panne de copie.

Le défaut s'est reproduit tel quel dans `fjbk` : **`argparse` sort en 2 sur
toute erreur d'usage**, et 2 veut dire « opération en échec » dans le contrat de
[§ 7](#7-la-sauvegarde). Il a été constaté avant d'être corrigé :

```
$ fjbk backup --foo ; echo $?
2                      ← avant
1                      ← après
```

Rabattu sur 1, comme l'ancien `offsite.py` rabattait tout code imprévu.
`--help` sort toujours en 0.

### `PGDATA` de l'image `postgres:18` — 22 août 2026

L'image `postgres:18` a déplacé son `PGDATA` par défaut sous
`/var/lib/postgresql/<version>/docker`. Un `compose.yaml` qui monte un volume
sur `/var/lib/postgresql/data` — la forme héritée des images précédentes —
**ne reçoit rien** : la base vit alors dans une couche jetable, perdue au
premier `docker compose down -v`, **sans le moindre message**.

`compose.yaml` déclare donc `PGDATA` explicitement. Pour vérifier qu'une pile
existante ne souffre pas du problème :

```bash
docker compose exec -T db psql -U forgejo -tAc 'SHOW data_directory'
# attendu : /var/lib/postgresql/data
```

### `sda` n'est pas `scsi0` — 23 août 2026

**Le quasi-accident de cette installation.** À la création de la VM 300, `lsblk`
donnait :

```
sda    80G                        ← scsi1, disque de données
sdb    20G  ├─sdb1 /  └─sdb15 /boot/efi   ← scsi0, LE SYSTÈME
sdc   200G                        ← scsi2, registre
```

La première version de ce runbook écrivait `mkfs.ext4 -L srv /dev/sdb` en dur,
en supposant que `scsi0`→`sda`. **Cette commande aurait détruit le système.**

Ce qui l'a évité : le `lsblk` demandé avant, et la consigne de vérifier par la
taille. Ce qui l'avait rendu possible : avoir écrit, deux lignes plus bas, une
commande copiable-collable qui contredisait cette consigne. Une procédure qui
dit « vérifiez » puis fournit la réponse toute faite sera copiée, pas vérifiée.

Le § 2 ne nomme donc plus aucun périphérique par sa lettre : il passe par
`/dev/disk/by-id/…drive-scsiN`, où le numéro de série reprend le slot Proxmox et
ne dépend pas de l'ordre d'énumération du noyau. Il ne donne plus non plus la
correspondance lettre → slot, même à titre indicatif : c'est la sortie de
`ls -l /dev/disk/by-id/` qui fait foi, et elle seule.

### Le même piège, retourné : la recréation en quatre disques — 23 août 2026

Le constat ci-dessus est celui du **montage à trois disques d'alors** — 80 Go de
données, 200 Go de registre. La machine a été recréée depuis, à quatre disques
([§ 1](#1-créer-la-vm)), et `lsblk` y donne :

```
sda    20G  ├─sda1 /  └─sda15 /boot/efi   ← scsi0, LE SYSTÈME
sdb    40G                                ← scsi1
sdc   100G                                ← scsi2
sdd    50G                                ← scsi3
```

**Cette fois les lettres suivent les slots.** La correspondance se déduit des
tailles — 20, 40, 100, 50 Go sont celles de `scsi0` à `scsi3`, et elles sont
toutes différentes — et non d'une règle : il n'y en a pas.

Et c'est le pire résultat possible. **La même VM, deux créations, deux ordres.**
Qui n'aurait vu que cette sortie-ci en tirerait « `scsi0` → `sda` », écrirait
une procédure qui marche, et détruirait le système la fois d'après. Le premier
constat sans le second laisserait croire à un ordre inversé stable ; les deux
ensemble disent la seule chose vraie : **il n'y a pas d'ordre**.

Il n'y a donc pas de correspondance à retenir. Il y a
`ls -l /dev/disk/by-id/ | grep drive-scsi` à relire, à chaque fois.

### `mkfs.ext4: command not found`, et le paquet est installé — 23 août 2026

Constaté au § 2, en `admin`, sur la VM 300 :

```
admin@forgejo:/opt$ mkfs.ext4 -L srv       "$SRV"
-bash: mkfs.ext4: command not found
```

**Le binaire est là.** `e2fsprogs` est un paquet essentiel de Debian — la racine
de la machine est elle-même en ext4, elle n'aurait pas pu être formatée sans :

```
$ ls -l /usr/sbin/mkfs.ext4
lrwxrwxrwx 1 root root 6 /usr/sbin/mkfs.ext4 -> mke2fs
$ /usr/sbin/mkfs.ext4 -V
mke2fs 1.47.2 (1-Jan-2025)
```

Ce qui manque, c'est le `PATH`. Sur Debian, `/usr/sbin` et `/sbin` n'y sont que
pour l'uid 0 ; un utilisateur ordinaire ne les a pas. Le défaut se reproduit
sans toucher à quoi que ce soit :

```
$ PATH=/usr/local/bin:/usr/bin:/bin bash -c "mkfs.ext4 --version"
bash: line 1: mkfs.ext4: command not found
$ echo $?
127
```

**Le message accuse l'installation alors que le fautif est le chemin de
recherche** — c'est la même famille de piège que les deux-points du `.pgpass` et
que le code 2 d'`argparse` : une erreur qui décrit fidèlement autre chose que sa
cause. La mauvaise réaction est `apt install e2fsprogs`, qui répondra que le
paquet est déjà là et laissera la question entière.

Et il n'y a **ni paquet à installer, ni `PATH` à exporter** : `sudo` a son
propre chemin de recherche, qui contient `/usr/sbin`.

```
Defaults	secure_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
```

Le § 2 préfixe donc par `sudo` tout ce qui vit dans `/usr/sbin` — `mkfs.ext4`,
`blkid` — et laisse `lsblk` et `findmnt` tels quels, qui sont dans `/usr/bin`.

> **La règle, sur cette VM** : tout se fait en `admin` par SSH. Un
> `command not found` sur un outil de disque ou de système de fichiers veut dire
> « il manque `sudo` », jamais « il manque le paquet ».

### La console série qui ne dit rien — 23 août 2026

Ouvrir la console d'une VM créée avec `--vga serial0` affiche :

```
starting serial terminal on interface serial0
```

**Ce message est normal** : c'est Proxmox qui attache son terminal au port
série. Ce n'est pas une erreur, et il n'est jamais suivi d'un « prêt ».

> **Constaté le 23 août 2026, à la création de la VM 300** : la console restait
> muette après cette ligne, et la VM allait parfaitement bien — `qm status`
> disait `running`, `ping 192.168.1.56` répondait en 0,3 ms, cloud-init avait
> appliqué l'IP et les clés. **Il fallait appuyer sur Entrée.** Une demi-heure
> de diagnostic pour un affichage qui n'avait pas été redemandé.

Si rien ne vient ensuite, dans cet ordre :

1. **Appuyer sur Entrée.** Une console série ne rejoue pas ce qui a été écrit
   avant qu'on s'y connecte : si la VM a fini de démarrer, l'invite de login est
   déjà passée et l'écran reste noir jusqu'à ce qu'on provoque un affichage.
   C'est la cause la plus fréquente, et la moins inquiétante.

2. Vérifier que la VM tourne et qu'elle a de quoi s'amorcer :

   ```bash
   qm status 300
   qm config 300 | grep -E 'scsi0|boot|serial|vga'
   # attendu : une ligne scsi0: qui pointe un volume de 20 Go sur local-lvm
   #           boot: order=scsi0
   #           serial0: socket
   #           vga: serial0
   ```

   Un `scsi0` absent ou pointant sur un volume vide donne exactement ce
   symptôme : la VM tourne, le firmware ne trouve rien à démarrer, et la
   console reste muette parce que le noyau n'a jamais été chargé.

   > La forme `--scsi0 local-lvm:0,import-from=…` du [§ 1](#1-créer-la-vm)
   > supprime toute une classe de cette panne : c'est Proxmox qui alloue le
   > volume et l'attache dans le même geste. L'ancienne forme en deux temps —
   > `qm disk import` puis `qm set --scsi0 <volid>` — obligeait à **recopier le
   > volid affiché**, qui n'est `-disk-0` que si le stockage ne porte pas déjà
   > un volume de cette VM. Un volid supposé donnait une VM qui démarre sur
   > rien, et une console qui ne dit jamais pourquoi.

3. **Vérifier que cloud-init est là.** C'est le piège qui ressemble le plus à
   une console morte alors que tout démarre :

   ```bash
   qm config 300 | grep -E 'ide2|ipconfig0|ciuser|sshkeys'
   # attendu : ide2: local-lvm:cloudinit,media=cdrom
   #           ciuser: admin
   #           ipconfig0: ip=192.168.1.56/24,gw=192.168.1.254
   ```

   Sans `ide2`, cloud-init n'a **aucune source de données** : pas d'utilisateur
   `admin`, pas de clé SSH, pas d'IP statique. La VM démarre très bien et
   s'arrête sur un `localhost login:` où personne ne peut entrer — root est
   verrouillé dans les images cloud. Vu de la console, cela se confond avec un
   démarrage raté.

   Le `ping` tranche l'autre moitié de la question :

   ```bash
   ping -c2 192.168.1.56
   ```

   S'il répond, cloud-init a fait son travail et la console n'était qu'un faux
   problème — il fallait appuyer sur Entrée.

   Et une fois **dans** la VM, la source de données se constate sans repasser
   par le nœud : elle porte l'étiquette `cidata`.

   ```bash
   lsblk -o NAME,SIZE,FSTYPE,LABEL | grep cidata
   # attendu : sr0   4M iso9660 cidata
   ```

   Ce contrôle-ci ne sert évidemment qu'après coup — il suppose qu'on ait pu
   entrer, ce qui est précisément ce qui manque quand `ide2` est absent. Il vaut
   pour confirmer, pas pour diagnostiquer.

4. En dernier recours, la console depuis le nœud plutôt que par l'interface
   web : elle est plus bavarde et ne dépend pas du navigateur.

   ```bash
   qm terminal 300      # puis Entrée ; on quitte par Ctrl-O
   ```

Et si la console finit par répondre : **elle ne sert à rien sans mot de passe.**
Voir [L'accès de secours](#laccès-de-secours).

### `set -e` et `[[ … ]] && die` — 22 août 2026

Dans `init.sh`, la forme `[[ -e $TEMOIN ]] && die "…"` fait sortir le script en
**1 quand le témoin est absent**, c'est-à-dire dans le cas normal : sous
`set -e`, une liste dont la première commande échoue fait échouer le script. Le
test est donc écrit en `if`. La forme `||` (`[[ … ]] || die`) est sûre, elle.
