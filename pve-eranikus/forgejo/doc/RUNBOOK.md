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
2. [Formater et étiqueter `/srv`](#2-formater-et-étiqueter-srv)
3. [Lancer `init.sh`](#3-lancer-initsh)
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
  --cores 2 --sockets 1 --cpu host \
  --memory 4096 \
  --net0 virtio,bridge=vmbr0 \
  --scsihw virtio-scsi-single \
  --ostype l26 \
  --serial0 socket --vga serial0 \
  --agent enabled=1 \
  --onboot 1 \
  --startup order=1

# Disque système : importer l'image cloud, puis l'étendre à 20 Go.
# `qm disk import` AFFICHE le volid qu'il a créé, sur sa dernière ligne :
#   successfully imported disk 'local-lvm:vm-300-disk-0'
# Ce n'est pas toujours -disk-0 — si le stockage porte déjà des volumes de
# cette VM, ce sera -disk-1. REPRENDRE CE QU'IL AFFICHE, ne pas le supposer :
# un scsi0 qui pointe à côté donne une VM qui démarre sur rien, et une console
# qui ne dira jamais rien (voir § 9).
qm disk import 300 /var/lib/vz/template/iso/debian-13-genericcloud-amd64.qcow2 local-lvm
qm set 300 --scsi0 local-lvm:vm-300-disk-0
qm disk resize 300 scsi0 20G

# SECOND DISQUE : /srv, 80 Go sur le pool ZFS « data ». Il porte les dépôts,
# la base ET les sauvegardes locales. SÉPARÉ du disque système pour que
# réinstaller le système ne touche pas aux données, et pour que saturer la
# racine n'arrête pas PostgreSQL.
# 80 Go n'est pas une valeur évidente : voir « Dimensionner /srv » ci-dessous.
qm set 300 --scsi1 data:80

# TROISIÈME DISQUE : le registre d'artefacts, 200 Go, avec backup=0.
# `backup=0` EST LE CŒUR DE LA DÉCISION : vzdump saute ce disque. Les artefacts
# ne sont pas sauvegardés — ni ici, ni par `fjbk`, ni vers GCS — parce qu'ils se
# reconstruisent depuis le code, et que le code, lui, est sauvegardé. Sans ce
# drapeau, chaque vzdump traînerait 200 Go de binaires reconstructibles et le
# PRA scénario 2 deviendrait inutilisable.
qm set 300 --scsi2 data:200,backup=0

# cloud-init
qm set 300 --ide2 local-lvm:cloudinit
qm set 300 --boot order=scsi0
qm set 300 --ciuser admin
# Les clés du nœud, pas un fichier dédié : voir « Les clés SSH » ci-dessous.
qm set 300 --sshkeys /root/.ssh/authorized_keys
qm set 300 --ipconfig0 ip=192.168.1.56/24,gw=192.168.1.254
qm set 300 --nameserver 192.168.1.254
qm set 300 --ciupgrade 0

qm start 300
```

### Pourquoi ces valeurs

| | |
|---|---|
| **2 vCPU, 4 Go** | Forgejo et PostgreSQL pour quelques utilisateurs. L'indexation d'un gros dépôt est le seul pic ; elle passe. |
| **`--cpu host`** | Pas de migration à froid vers un autre modèle de processeur à prévoir : ce nœud est seul. |
| **Disque système 20 Go** | Le système, les images Docker et les journaux. Les données n'y sont pas. |
| **Trois disques, pas un** | Chacun a un cycle de vie différent : le système se réinstalle, `/srv` se sauvegarde, le registre se reconstruit. Et surtout : remplir l'un ne peut pas arrêter les autres — un registre saturé n'empêche pas PostgreSQL d'écrire son WAL. |
| **`--onboot 1`, `--startup order=1`** | La source de vérité remonte la première après une coupure. Il n'y a plus d'ordre à respecter entre deux conteneurs : la base est dans la VM. |
| **`--agent enabled=1`** | `qm shutdown` obtient un arrêt propre plutôt qu'une coupure d'alimentation. Nécessaire aussi pour qu'un `qm snapshot` sache geler le système de fichiers. |
| **`--ciupgrade 0`** | cloud-init ne met pas à jour au premier démarrage : `init.sh` le fait, et il est le seul à décider quand. |

### Dimensionner `/srv`

Trois choses partagent ce volume, et **ce n'est pas la plus grosse qui
contraint** :

| | |
|---|---|
| `/srv/forgejo/data` | dépôts, LFS, pièces jointes — appelons-le **R**. Le registre d'artefacts n'y est PAS, il a son disque. |
| `/srv/forgejo/db` | PostgreSQL : tickets, comptes, métadonnées. Quelques centaines de Mo, et il ne bouge quasiment pas. |
| `/srv/forgejo/backups` | **7 paires**, soit environ **7 × R comprimé** |

La rétention locale de sept jours est le terme dominant : à 5 Go de dépôts, les
sauvegardes locales pèsent déjà une vingtaine de Go. **80 Go tiennent donc de
l'ordre de 10 Go de dépôts, pas 80.**

Trois leviers, dans l'ordre où on les tire :

1. **Agrandir le volume.** En ligne, sans rien arrêter — c'est le premier
   levier, et c'est pour ça que se tromper à la création coûte peu :

   ```bash
   qm disk resize 300 scsi1 +40G     # sur le NŒUD
   sudo resize2fs /dev/sdb           # dans la VM, /srv reste monté
   df -h /srv
   ```

   Pas de table de partitions à décaler : le système de fichiers occupe le
   disque entier ([§ 2](#2-formater-et-étiqueter-srv)). Le disque du registre
   s'agrandit exactement pareil, en visant `scsi2` et `/dev/sdc`.

2. **Baisser la rétention locale** — `FJBK_RETENTION=3` dans
   `/etc/default/fjbk`. L'historique long vit dans GCS ; le local n'est là que
   pour restaurer vite, sans rapatrier.

3. **Sortir `backups/` sur un quatrième disque**, pour que saturer les
   sauvegardes ne puisse jamais empêcher PostgreSQL d'écrire.

`fjbk backup` refuse de démarrer sous **4 Gio libres** (`FJBK_MIN_LIBRE_MO`) :
mieux vaut une sauvegarde qui manque bruyamment qu'un `/srv` plein, qui
arrêterait la base faute de pouvoir écrire son WAL.

### Le cas des artefacts

Forgejo sert ici de registre de paquets — **conteneurs OCI, Java, npm, Go** — et
ce registre est **délibérément hors de la sauvegarde**. C'est une décision, pas
un oubli, et elle est tenue par trois mécanismes plutôt que par une intention.

**Ce qui est sauvegardé : le code. Ce qui ne l'est pas : ce qui se reconstruit
depuis le code.** Un artefact perdu se republie ; un dépôt perdu ne se retrouve
nulle part. Sauvegarder les deux au même niveau reviendrait à payer le RTO du
second sur le volume du premier — et sur un registre d'images, ce volume écrase
tout le reste d'un facteur dix ou cent.

Trois mécanismes, et c'est ce qui empêche la décision de se déliter :

| | |
|---|---|
| `FORGEJO__storage_0X2E_packages__PATH: /packages` | le registre vit **hors de `/data`**. Par défaut il serait sous `APP_DATA_PATH/packages`, donc happé par le `tar` nocturne de `fjbk` sans que personne ne le décide. |
| Un **disque dédié** monté sur `/srv/packages` | remplir le registre ne peut pas remplir `/srv`, donc ne peut pas arrêter PostgreSQL. |
| `backup=0` sur ce disque | **vzdump le saute.** Sans ce drapeau, chaque sauvegarde de VM traînerait des centaines de Go de binaires reconstructibles, et le [PRA scénario 2](PRA.md#2--vm-cassée) deviendrait inutilisable. |

**Ce que ça coûte le jour d'un sinistre** : après une reprise, **le registre est
vide**. Les images doivent être republiées — par la CI, ou à la main depuis les
postes. C'est écrit dans [le PRA](PRA.md#ce-quon-perd-et-ce-quon-ne-perd-pas),
et c'est le prix accepté.

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
**si le disque du registre n'est pas monté**, `/srv/packages` existe quand même
comme simple répertoire de `/srv`, Forgejo démarre sans rien dire, et les
artefacts vont s'entasser sur le volume des dépôts jusqu'à le remplir.

D'où le contrôle `paquets` de `fj-check.py`, qui vérifie que c'est bien un
**point de montage** et non un répertoire :

```
  [KO ] paquets      /srv/packages N'EST PAS UN POINT DE MONTAGE — les artefacts
                     vont sur le disque des dépôts (mount /srv/packages)
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

## 2. Formater et étiqueter `/srv`

> **DESTRUCTIF. À taper à la main, une seule fois, sur une VM neuve.**
> C'est le seul geste de tout ce montage qui puisse détruire les dépôts.
> `init.sh` ne le fait pas et ne le fera jamais : il vérifie que l'étiquette
> existe, et refuse de continuer sinon.

```bash
# Dans la VM, en root. VÉRIFIER D'ABORD lequel est lequel : ils n'ont pas la
# même taille, et c'est le seul moyen sûr de ne pas les intervertir.
lsblk
# attendu : sdb (ou vdb) 80G   → /srv     : dépôts, base, sauvegardes
#           sdc (ou vdc) 200G  → registre : artefacts, NON sauvegardé
# les deux sans partition ni point de montage

mkfs.ext4 -L srv      /dev/sdb
mkfs.ext4 -L packages /dev/sdc

blkid -L srv         # doit répondre /dev/sdb
blkid -L packages    # doit répondre /dev/sdc
```

**Intervertir les deux étiquettes est le pire scénario de cette section** : les
dépôts partiraient sur le disque `backup=0`, donc hors de tout vzdump, et le
registre occuperait le volume sauvegardé. Rien ne le signalerait avant le jour
où l'on chercherait une sauvegarde. D'où la vérification par la taille.

**L'étiquette, pas le nom de périphérique.** L'ordre d'énumération des disques
n'est pas garanti d'un démarrage à l'autre ; un `/etc/fstab` qui nomme `/dev/sdb`
peut monter le disque système sur `/srv` et remplir la racine. `init.sh` écrit
donc `LABEL=srv` dans `fstab`, et rien d'autre.

Pas de partition : le système de fichiers occupe le disque entier. Un disque
virtuel s'agrandit par `qm disk resize` puis `resize2fs`, sans table de
partitions à décaler.

---

## 3. Lancer `init.sh`

```bash
# Depuis le poste, une fois le dépôt cloné dans la VM (voir § 4)
ssh admin@192.168.1.56
sudo /opt/homelab/pve-eranikus/forgejo/scripts/init.sh
```

Il pose : mise à jour du système, montage de `/srv` par étiquette, dépôt Docker
CE officiel (la clé dans `/etc/apt/keyrings`) puis `docker-ce docker-ce-cli
containerd.io docker-compose-plugin`, `rclone`, la rotation des journaux Docker,
`admin` dans le groupe `docker`, les mises à jour automatiques restreintes à la
sécurité, le fuseau horaire, `qemu-guest-agent`.

### Le témoin

`init.sh` écrit `/var/lib/homelab/init.done` — la date ISO de fin — **à la
toute fin**, et refuse de repartir si le fichier existe :

```
14:22:07 [ERROR] déjà provisionné le 2026-08-22T14:20:11+02:00 — voir doc/RUNBOOK.md section 3
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

```bash
# Dans la VM
sudo mkdir -p /opt/homelab
sudo chown admin:admin /opt/homelab
git clone git@forgejo.wittner.tech:homelab/homelab_proxmox.git /opt/homelab
```

La clé de déploiement est **en lecture seule** : cette VM n'a aucune raison de
pouvoir écrire dans le dépôt qu'elle sert. Pendant la transition, le clone se
fait depuis le miroir GitHub — voir [§ 8](#8-la-boucle-assumée).

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

# 5. Sur le NŒUD, une fois sûr — un snapshot oublié grossit en silence
qm delsnapshot 300 avant-forgejo-15-0-8
```

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
3. `tar czf` de `/srv/forgejo/data` → `data-<horodatage>.tar.gz`, en excluant
   `data/gitea/log` et `data/gitea/indexers` (Forgejo les reconstruit). **Le
   registre d'artefacts n'y est pas** : il vit hors de `/data`, sur son disque,
   et n'est pas sauvegardé — voir [Le cas des artefacts](#le-cas-des-artefacts)
4. **redémarrage de Forgejo, dans un `finally`, quoi qu'il arrive**
5. `rclone copy` de la paire vers GCS
6. purge locale au-delà de 7 jours
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

- [ ] clé de déploiement GitHub créée, clone initial fait
- [ ] dépôt migré dans Forgejo, miroir push configuré et vérifié
- [ ] clé de déploiement Forgejo créée, `git remote set-url` fait dans la VM
- [ ] **clé de déploiement GitHub supprimée** — celle-ci, pas le miroir

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
   # attendu : scsi0: local-lvm:vm-300-disk-0,size=20G
   #           boot: order=scsi0
   #           serial0: socket
   #           vga: serial0
   ```

   Un `scsi0` absent ou pointant sur un volume vide donne exactement ce
   symptôme : la VM tourne, le firmware ne trouve rien à démarrer, et la
   console reste muette parce que le noyau n'a jamais été chargé. Voir la
   remarque sur le volid de `qm disk import` en [§ 1](#1-créer-la-vm).

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
