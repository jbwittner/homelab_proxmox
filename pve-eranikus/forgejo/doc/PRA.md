# PRA — VM Forgejo (`pve-eranikus`, VMID 300)

**Une procédure complète par scénario.** Ce document se répète volontairement :
en reprise on ne lit pas un document, on va à son cas et on doit y trouver tout
ce qu'il faut sans naviguer. C'est le seul endroit du dépôt où la duplication
est un choix.

## Trouver son scénario

| Ce qu'on constate | Scénario |
|---|---|
| `https://forgejo.wittner.tech/` ne répond pas, mais `ssh admin@192.168.1.56` passe | [1 — Forgejo indisponible, VM saine](#1--forgejo-indisponible-vm-saine) |
| La VM ne démarre plus, ou démarre cassée après une mise à jour | [2 — VM cassée](#2--vm-cassée) |
| `pve-eranikus` ne répond plus du tout | [3 — Nœud perdu / sinistre](#3--nœud-perdu--sinistre) |
| Forgejo est mort **et j'ai besoin du dépôt maintenant** | [4 — Forgejo est mort et j'ai besoin du dépôt](#4--forgejo-est-mort-et-jai-besoin-du-dépôt) |

## Ce qu'on perd, et ce qu'on ne perd pas

| | |
|---|---|
| **RPO** | **24 h.** La paire part chaque nuit à 3 h (± 10 min). Tout ce qui a été poussé depuis la dernière sauvegarde réussie est perdu — sauf s'il est encore dans un clone local ou sur le miroir GitHub. |
| **RTO** | **À MESURER.** Laissé vide tant qu'un exercice ne l'a pas chronométré — voir [PRA-exercice.md](PRA-exercice.md). Une durée estimée de tête n'a aucune valeur le jour où on en a besoin. |
| Ce qui survit ailleurs | les objets git des dépôts **miroités** sur GitHub ; les clones locaux sur les postes ; les paires de sauvegarde dans GCS |
| Ce qui ne survit **que** dans la paire | les tickets, les demandes d'ajout, les comptes, les clés SSH des utilisateurs, les jetons, les dépôts non miroités |
| Le registre d'artefacts | **dans le vzdump, et nulle part ailleurs.** Il est sur le chemin critique du démarrage d'ArgoCD, donc il est sauvegardé — mais par le vzdump du nœud, pas par la paire nocturne. Conséquence directe : [scénario 2](#2--vm-cassée) le retrouve intact, [scénario 3](#3--nœud-perdu--sinistre) repart avec un **registre vide** tant que les vzdump ne sont pas répliqués hors du nœud. |

## Ce que ce plan ne couvre pas

- **La perte des quatre secrets.** Ce n'est pas un scénario de reprise : c'est
  un dégât irréversible. Une instance reconstruite avec un autre `SECRET_KEY`
  démarre parfaitement et ne peut plus déchiffrer les jetons de miroir stockés
  en base — ils échouent **en silence**. Les secrets vivent chiffrés par sops
  hors de la VM, et c'est la seule protection.
- **La perte simultanée du nœud et du bucket GCS.** Il reste alors les clones
  locaux et le miroir GitHub, c'est-à-dire les objets git et rien d'autre.
- **La compromission.** Restaurer une sauvegarde compromise restaure la
  compromission.
- **Le registre d'artefacts hors du nœud.** Il est dans le vzdump — donc
  protégé tant que le nœud va bien — mais **ni dans la paire, ni dans GCS**. Un
  vzdump qui reste sur le nœud disparaît avec lui. Tant que les vzdump ne sont
  pas répliqués ailleurs, le scénario 3 repart sans registre, et il faut
  republier les images avant qu'ArgoCD ne puisse démarrer les applications —
  [pourquoi](RUNBOOK.md#le-cas-des-artefacts).

---

## 1 — Forgejo indisponible, VM saine

La VM répond en SSH, `https://forgejo.wittner.tech/` non.

### Constater

```bash
ssh admin@192.168.1.56
cd /opt/homelab/pve-eranikus/forgejo
./scripts/fj-check.py
```

Six lignes sortent, une par contrôle. La première qui porte `KO` désigne la
suite.

```bash
docker compose ps
docker compose logs --tail=100 forgejo
docker compose logs --tail=50 db
```

### Cas A — un conteneur est arrêté

```bash
docker compose up -d
sleep 20
./scripts/fj-check.py
```

### Cas B — la base ne répond pas

```bash
docker compose exec -T db pg_isready -U forgejo -d forgejo
docker compose logs --tail=100 db
```

Si le journal parle d'un répertoire de données vide ou absent, **vérifier
d'abord `PGDATA`** — c'est le piège de l'image `postgres:18` :

```bash
docker compose exec -T db psql -U forgejo -tAc 'SHOW data_directory'
# attendu : /var/lib/postgresql/data
mountpoint /srv && ls -la /srv/forgejo/db | head
```

Si `/srv` n'est pas monté, c'est la cause et rien d'autre ne servira :

```bash
sudo mount /srv
docker compose up -d
```

### Cas C — Forgejo boucle au démarrage

Presque toujours un secret absent. La pile refuse alors de partir en nommant la
clé manquante :

```
error while interpolating services.forgejo.environment.FORGEJO__security__SECRET_KEY:
required variable FORGEJO_SECRET_KEY is missing a value
```

Le `.env` a disparu ou a été tronqué. Le reposer **depuis le poste**, jamais
depuis un `git pull` :

```bash
# Sur le POSTE
sops -d forgejo.env.sops > /tmp/.env
scp /tmp/.env admin@192.168.1.56:/opt/homelab/pve-eranikus/forgejo/.env
shred -u /tmp/.env
# Dans la VM
docker compose up -d
```

### Cas D — le disque est plein

**Lequel ?** Les trois volumes sont séparés, et la réponse n'est pas la même :

```bash
df -h / /srv/forgejo /srv/artifacts /srv/backup
sudo /opt/homelab/pve-eranikus/forgejo/scripts/fjbk list
```

| Plein | Ce que ça arrête | Quoi faire |
|---|---|---|
| `/srv/forgejo` | **PostgreSQL**, faute de pouvoir écrire son WAL | agrandir : `qm disk resize 300 scsi1 +20G` sur le nœud, puis `sudo resize2fs "$(findmnt -no SOURCE /srv/forgejo)"` |
| `/srv/backup` | la sauvegarde seulement — `fjbk backup` refuse sous 4 Gio | purger d'anciennes paires, ou `FJBK_RETENTION=3` dans `/etc/default/fjbk` |
| `/srv/artifacts` | les publications d'images | agrandir (`scsi2`), et écrire la politique de rétention du registre |
| `/` | Docker, donc tout | `docker image prune -f` — c'est presque toujours des couches d'images ([runbook § 5](RUNBOOK.md#5-mettre-à-jour-forgejo)) |

Purger d'anciennes paires locales (le distant n'est jamais touché par ce code —
il relève du cycle de vie du bucket) :

```bash
sudo rm /srv/backup/db-<vieil-horodatage>.dump
sudo rm /srv/backup/data-<vieil-horodatage>.tar.gz
```

### Cas E — `fj-check.py` dit qu'un volume n'est pas monté

```
  [KO ] montages     /srv/artifacts (registre) N'EST PAS MONTÉ — les monter avant toute écriture
```

**À traiter tout de suite, même si le site répond.** Tant que le volume n'est
pas monté, son répertoire existe quand même — sur le **disque système de
20 Go** — et tout ce qui devait aller dessus s'y entasse en silence : les
artefacts, les dépôts, ou les paires de sauvegarde, jusqu'à remplir la racine.

```bash
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINTS
# -c /dev/null : sans lui, le cache de blkid peut être en retard.
blkid -c /dev/null -L srv          # 40 Go  → /srv/forgejo
blkid -c /dev/null -L artifacts    # 100 Go → /srv/artifacts
blkid -c /dev/null -L backup       # 50 Go  → /srv/backup
sudo mount /srv/artifacts          # ou le point que fj-check.py a nommé
./scripts/fj-check.py
```

Si une étiquette ne répond pas, ce n'est plus un montage à refaire mais un
disque à retrouver : `qm config 300 | grep scsi` sur le nœud.

Ce qui a déjà été écrit au mauvais endroit est **caché** sous le point de
montage une fois celui-ci monté : le récupérer demande de démonter, de déplacer,
puis de remonter.

```bash
sudo umount /srv/artifacts
sudo du -sh /srv/artifacts         # ce qui s'est écrit sur le disque système
# les déplacer si l'on y tient — sinon les supprimer, ils se republient
sudo rm -rf /srv/artifacts/*
sudo mount /srv/artifacts
```

> **S'il s'agissait de `/srv/forgejo`, ne rien supprimer** : ce ne sont pas des
> artefacts republiables, ce sont des dépôts et une base. Démonter, déplacer le
> contenu vers le volume monté, puis remonter — ou restaurer une paire.

### Cas F — les données sont corrompues

Passer à une restauration de paire, la pile étant saine :

```bash
sudo /opt/homelab/pve-eranikus/forgejo/scripts/fjbk list
sudo /opt/homelab/pve-eranikus/forgejo/scripts/fjbk verify <horodatage>
sudo /opt/homelab/pve-eranikus/forgejo/scripts/fjbk restore <horodatage>
# demande de retaper l'horodatage pour confirmer
```

---

## 2 — VM cassée

Une mise à jour a mal tourné, le système ne démarre plus, ou démarre dans un
état inutilisable. **Le nœud, lui, va bien.**

### D'abord : le snapshot, s'il existe

Une mise à jour de Forgejo ou du système est censée être précédée d'un
`qm snapshot`. S'il est encore là, c'est le chemin le plus court :

```bash
# Sur le NŒUD pve-eranikus
qm listsnapshot 300
qm rollback 300 avant-forgejo-15-0-8
qm start 300
```

Le rollback est **le seul moyen de revenir en arrière sur une migration de
schéma Forgejo** : remettre l'ancienne image de conteneur sur une base déjà
migrée ne fonctionne pas.

### Sinon : le vzdump local

```bash
# Sur le NŒUD — voir ce qu'on a
ls -lh /var/lib/vz/dump/ | grep 300
```

```bash
# Arrêter la VM si elle tourne encore
qm stop 300

# Restaurer. --force écrase la VM 300 existante : vérifier le fichier AVANT.
qmrestore /var/lib/vz/dump/vzdump-qemu-300-<date>.vma.zst 300 --force 1

qm start 300
```

### Vérifier, et seulement ensuite conclure

```bash
ssh admin@192.168.1.56
cd /opt/homelab/pve-eranikus/forgejo
./scripts/fj-check.py
```

Le vzdump porte la VM **presque entière** : le système, les dépôts, la base
**et le registre d'artefacts** — la base et les dépôts sont cohérents entre eux,
il n'y a pas de second temps, et les images d'ArgoCD sont là. Le seul disque
qu'il ne porte pas est `/srv/backup` (`backup=0`), qui ne contient que des
sauvegardes dont la copie qui compte est chez GCS. Si le vzdump est
plus vieux que la dernière paire de sauvegarde, rattraper avec :

```bash
sudo /opt/homelab/pve-eranikus/forgejo/scripts/fjbk list
sudo /opt/homelab/pve-eranikus/forgejo/scripts/fjbk restore <horodatage-plus-récent>
```

---

## 3 — Nœud perdu / sinistre

`pve-eranikus` ne répond plus. **Tout est à refaire sur un autre nœud.**

> **C'est le scénario à chronométrer** — voir [PRA-exercice.md](PRA-exercice.md).
> Il est écrit ici **en entier**, commandes comprises : le jour où on le joue,
> on ne va pas chercher le runbook.

**Traefik survit** : il est sur `pve-ysera` (CT 201, 192.168.1.50). Le routage
tient debout et pointe vers un dos mort ; il recommencera à servir dès qu'une
machine reprendra l'IP `192.168.1.56`, **sans qu'aucune configuration Traefik ne
soit à toucher**.

### Ce qu'on a ailleurs

| Où | Quoi |
|---|---|
| GCS, bucket dédié | les paires `db-*.dump` + `data-*.tar.gz`, jusqu'à `<= 24 h` |
| Miroir GitHub | les objets git des dépôts qui y sont poussés |
| Poste de travail | le clone de ce dépôt, et le `.env` chiffré par sops |
| sops | les quatre secrets **et** le mot de passe de la base |

### 3.1 — Récupérer l'image Debian

```bash
# Sur le nœud de repli
cd /var/lib/vz/template/iso
wget https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2
wget https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS
sha512sum --ignore-missing -c SHA512SUMS
```

Variante **`genericcloud`**, pas `generic`.

### 3.2 — Recréer la VM

**Reprendre l'IP `192.168.1.56`** : Traefik y route déjà. En changer demande de
toucher aussi à `pve-ysera/traefik/dynamic/forgejo.yaml`, puis de commiter — ce
qui, dans ce scénario, suppose un Forgejo qui tourne. Autant reprendre l'IP.

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

# ── scsi0 — SYSTÈME, 20 Go, sauvegardé ───────────────────────────────────────
# Debian + Docker + /var/lib/docker (images, couches, journaux des conteneurs).
# Réinstallable de zéro : il ne porte AUCUNE donnée irremplaçable.
qm set 300 --scsi0 local-lvm:0,import-from=/var/lib/vz/template/iso/debian-13-genericcloud-amd64.qcow2,discard=on,ssd=1
qm disk resize 300 scsi0 20G

# ── scsi1 — /srv/forgejo, 40 Go, sauvegardé ──────────────────────────────────
# Les dépôts Git, le LFS, les pièces jointes ET la base PostgreSQL. UN SEUL
# disque pour les deux, délibérément : « base et dépôts restaurés au même
# instant » est toute l'architecture.
qm set 300 --scsi1 data:40,discard=on,ssd=1

# ── scsi2 — /srv/artifacts, 100 Go, SAUVEGARDÉ ───────────────────────────────
# Le registre de paquets. Sauvegardé par le vzdump — ArgoCD tire ses images
# d'ici. Dans CE scénario il repartira vide : le vzdump était sur le nœud perdu.
qm set 300 --scsi2 data:100,discard=on,ssd=1

# ── scsi3 — /srv/backup, 50 Go, backup=0 ─────────────────────────────────────
# Les 7 dernières paires de `fjbk backup`. backup=0 : ces fichiers SONT déjà une
# sauvegarde, et la copie qui compte est chez GCS.
qm set 300 --scsi3 data:50,backup=0,discard=on,ssd=1

# ── cloud-init ───────────────────────────────────────────────────────────────
qm set 300 --ide2 local-lvm:cloudinit
qm set 300 --boot order=scsi0
qm set 300 --ciuser admin
# authorized_keys DU NŒUD : il est forcément là, puisqu'il a fallu s'y
# connecter pour taper ceci. Un fichier de clé dédié serait une dépendance de
# plus à avoir recopiée sur le nœud de repli.
qm set 300 --sshkeys /root/.ssh/authorized_keys
qm set 300 --ipconfig0 ip=192.168.1.56/24,gw=192.168.1.254
qm set 300 --nameserver 192.168.1.254
qm set 300 --ciupgrade 0

qm start 300
```

### 3.3 — Formater les trois volumes de données

> **DESTRUCTIF, et c'est le geste le plus dangereux de toute la reprise.**

Trois disques, trois étiquettes, trois points de montage **frères** — `/srv`
lui-même reste sur le disque système :

| Slot | Taille | Étiquette | Monté sur |
|---|---|---|---|
| `scsi1` | 40 Go | `srv` | `/srv/forgejo` |
| `scsi2` | 100 Go | `artifacts` | `/srv/artifacts` |
| `scsi3` | 50 Go | `backup` | `/srv/backup` |

**N'écrivez jamais `/dev/sdX` de mémoire.** L'ordre d'énumération SCSI ne suit
pas les numéros de slot Proxmox : sur la VM 300, `scsi0` — le disque système —
avait pris `sdb`, et le disque de données `sda`. Un `mkfs` sur la lettre
supposée détruit le système au lieu de préparer les données.

```bash
ssh admin@192.168.1.56
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINTS
# Le disque système se reconnaît à ses partitions et à ses points de montage
# (/ et /boot/efi). Les trois autres sont nus : 40, 100 et 50 Go.
```

Viser les slots, pas les lettres — et c'est la sortie ci-dessous qui fait foi,
pas ce document :

```bash
ls -l /dev/disk/by-id/ | grep 'drive-scsi'

SRV=/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_drive-scsi1     # 40 Go  → /srv/forgejo
ART=/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_drive-scsi2     # 100 Go → /srv/artifacts
BKP=/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_drive-scsi3     # 50 Go  → /srv/backup

# DERNIER CONTRÔLE : les tailles doivent être celles-ci, et aucun des trois ne
# doit porter la moindre partition.
lsblk "$SRV" "$ART" "$BKP"

sudo mkfs.ext4 -L srv       "$SRV"
sudo mkfs.ext4 -L artifacts "$ART"
sudo mkfs.ext4 -L backup    "$BKP"
```

**Ne pas intervertir les étiquettes.** `srv` sur le disque `backup=0` mettrait
les dépôts hors de tout vzdump, et personne ne le verrait avant d'en avoir
besoin. D'où la vérification par **la taille en face de l'étiquette** :

```bash
# -c /dev/null : sans lui, le cache de blkid peut être en retard sur le mkfs
# et donner un refus qui accuse le disque à tort.
sudo blkid -c /dev/null -L srv          # doit répondre le disque de 40 Go
sudo blkid -c /dev/null -L artifacts    # celui de 100 Go
sudo blkid -c /dev/null -L backup       # celui de 50 Go
sudo lsblk -o NAME,SIZE,FSTYPE,LABEL    # la vue d'ensemble
```

`init.sh` écrira lui-même les trois lignes de `/etc/fstab` — par étiquette,
jamais par lettre :

```
LABEL=srv       /srv/forgejo   ext4 defaults 0 2
LABEL=artifacts /srv/artifacts ext4 defaults 0 2
LABEL=backup    /srv/backup    ext4 defaults 0 2
```

### 3.4 — Poser le dépôt et provisionner

**Le clone se fait depuis GitHub, pas depuis Forgejo** — Forgejo est justement
ce qu'on est en train de reconstruire.

`git` n'est PAS sur l'image `genericcloud`, et c'est `init.sh` qui l'installe :
le script arrive donc par `scp`, avant le clone. Il est autonome.

```bash
# Depuis le POSTE, à la racine du clone local
scp pve-eranikus/forgejo/scripts/init.sh admin@192.168.1.56:/tmp/
```

```bash
# Dans la VM
sudo bash /tmp/init.sh

# git est là maintenant : cloner DEPUIS GITHUB, pas depuis Forgejo, et en
# HTTPS — délibérément. Une clé de déploiement suppose une interface web où la
# déclarer ; en reprise, c'est une dépendance de plus, et celle de Forgejo est
# précisément ce qu'on reconstruit.
sudo mkdir -p /opt/homelab
sudo chown admin:admin /opt/homelab
git clone https://github.com/<org>/homelab_proxmox.git /opt/homelab
```

`init.sh` a produit au passage une **nouvelle** paire de clés de déploiement,
propre à cette machine — elle est neuve, donc inconnue de Forgejo. Elle se
déclare en lecture seule une fois l'instance remontée
([runbook § 4](RUNBOOK.md#le-dépôt-par-clé-de-déploiement-en-lecture-seule)), et
seulement si l'on veut repasser le clone en SSH. Ce n'est pas sur le chemin
critique de la reprise.

`init.sh` installe Docker, `git` et rclone, et monte les **trois** volumes par
étiquette. Il refuse de tourner si l'une des trois — `srv`, `artifacts`,
`backup` — ne répond pas à `blkid`, c'est-à-dire si l'étape 3.3 a été sautée ou
faite à moitié :

```
14:19:02 [ERROR] aucun volume étiqueté « backup » — le formater d'abord, voir doc/RUNBOOK.md section 2
```

Se déconnecter et se reconnecter pour que l'appartenance au groupe `docker`
prenne effet.

### 3.5 — Reposer les secrets

```bash
# Sur le POSTE
sops -d forgejo.env.sops > /tmp/.env
scp /tmp/.env admin@192.168.1.56:/opt/homelab/pve-eranikus/forgejo/.env
shred -u /tmp/.env
```

**Les mêmes quatre secrets qu'avant, pas des nouveaux.** Un `SECRET_KEY`
différent rend illisibles les jetons de miroir stockés dans la base qu'on
s'apprête à restaurer.

Reposer aussi la clé du compte de service GCS, qui sert à rapatrier la paire :

```bash
# Sur le POSTE
sops -d rclone-forgejo.json.sops > /tmp/k.json
scp /tmp/k.json admin@192.168.1.56:/tmp/k.json
shred -u /tmp/k.json
# Dans la VM
sudo mkdir -p /root/.config/rclone
sudo mv /tmp/k.json /root/.config/rclone/forgejo-backups.json
sudo chmod 600 /root/.config/rclone/forgejo-backups.json
sudo tee /root/.config/rclone/rclone.conf >/dev/null <<'EOF'
[gcs]
type = google cloud storage
service_account_file = /root/.config/rclone/forgejo-backups.json
EOF
sudo tee /etc/default/fjbk >/dev/null <<'EOF'
FJBK_BUCKET=<le-bucket-dédié>
FJBK_PREFIX=forgejo
FJBK_RETENTION=7
EOF
```

### 3.6 — Démarrer la pile vide

`fjbk restore` a besoin du conteneur `db` pour rejouer le dump : la pile doit
tourner avant la restauration.

```bash
cd /opt/homelab/pve-eranikus/forgejo
docker compose pull
docker compose up -d
docker compose ps          # attendre que db soit « healthy »
```

### 3.7 — Rapatrier et restaurer

```bash
sudo /opt/homelab/pve-eranikus/forgejo/scripts/fjbk list
# les paires distantes s'affichent avec « complet » ou « INCOMPLET »

sudo /opt/homelab/pve-eranikus/forgejo/scripts/fjbk restore <horodatage>
```

`restore` rapatrie ce qui manque localement, arrête Forgejo, recrée la base,
rejoue le dump, extrait les dépôts, repose les propriétaires en `1000:1000`,
redémarre la pile et appelle `fj-check.py`. Il **demande de retaper
l'horodatage** avant d'écraser quoi que ce soit.

Si le rapatriement est long et qu'on préfère éprouver la paire d'abord :

```bash
sudo /opt/homelab/pve-eranikus/forgejo/scripts/fjbk verify <horodatage>
```

### 3.8 — Vérifier le routage

**Si l'IP `192.168.1.56` a été reprise, il n'y a rien à faire** :
`https://forgejo.wittner.tech/` répond dès que le service démarre.

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://forgejo.wittner.tech/
```

Sinon, corriger l'adresse du backend — **deux endroits dans le même fichier**,
HTTP et TCP/SSH — dans `pve-ysera/traefik/dynamic/forgejo.yaml`, puis commiter.
Traefik surveille son répertoire dynamique et recharge seul.

```yaml
    forgejo:
      loadBalancer:
        servers:
          - url: "http://<nouvelle-ip>:3000"
...
    forgejo-ssh:
      loadBalancer:
        servers:
          - address: "<nouvelle-ip>:2222"
```

### 3.9 — Remettre la sauvegarde en marche

Une reprise n'est pas finie tant que la machine restaurée ne se sauvegarde pas.

```bash
sudo install -m 0644 /opt/homelab/pve-eranikus/forgejo/scripts/fjbk.service \
                     /opt/homelab/pve-eranikus/forgejo/scripts/fjbk.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fjbk.timer
sudo systemctl list-timers fjbk.timer
```

### 3.10 — Recette

```bash
cd /opt/homelab/pve-eranikus/forgejo && ./scripts/fj-check.py
```

Les six contrôles doivent être au vert — dont `montages`, qui dit que les trois
volumes sont bien montés et non simplement présents.

**Le registre, lui, est vide, et il faut savoir pourquoi** : il est sauvegardé,
mais par le vzdump du nœud — celui-là même qu'on vient de perdre. Republier les
images **avant** de compter sur ArgoCD, puisqu'il y tire les siennes :

```bash
docker push forgejo.wittner.tech/<org>/<image>:<tag>
```

Puis, **depuis une machine du LAN et non depuis le nœud** — le but est
d'éprouver le chemin complet, Traefik compris :

```bash
git clone https://forgejo.wittner.tech/<org>/<dépôt>.git
git clone ssh://git@forgejo.wittner.tech:2222/<org>/<dépôt>.git
```

Le second échoue tant que l'entryPoint `ssh` de Traefik n'a pas été pris en
compte : il est **statique**, donc il demande un redémarrage de Traefik.

Enfin : rebrancher les miroirs push, et vérifier qu'ils passent. S'ils échouent
en silence, le `SECRET_KEY` restauré n'est pas celui d'origine.

---

## 4 — Forgejo est mort et j'ai besoin du dépôt

Le cas qui rend la circularité tenable. **Aucune commande de cette procédure
n'interroge Forgejo.** En particulier : **pas de `git pull`.**

C'est le point de sortie de [la boucle assumée](RUNBOOK.md#8-la-boucle-assumée),
et il ne fonctionne que si on ne cherche pas à la refermer.

### Le point de départ : le clone local

Un clone git est une copie complète de l'historique. Celui qui est sur le poste
de travail suffit à tout.

```bash
# Sur le POSTE
cd ~/workspace/homelab_proxmox
git log --oneline -5          # vérifier qu'il est à jour de ce qu'on croit
```

S'il n'y en a pas sous la main, le miroir GitHub en fournit un :

```bash
git clone https://github.com/<org>/homelab_proxmox.git
```

### Remonter la pile ailleurs, sans Forgejo

Sur n'importe quelle machine avec Docker — un poste, une VM jetable, un autre
nœud :

```bash
# Sur le POSTE — envoyer le nécessaire, et rien d'autre
scp forgejo/compose.yaml admin@<machine>:~/forgejo/
sops -d forgejo.env.sops > /tmp/.env
scp /tmp/.env admin@<machine>:~/forgejo/.env
shred -u /tmp/.env
```

```bash
# Sur la machine
cd ~/forgejo
sudo mkdir -p /srv/forgejo/data /srv/forgejo/db
docker compose up -d
```

La pile démarre **vide**. Pour y remettre les données, rapatrier une paire
depuis GCS et jouer `fjbk restore` — le script est dans le clone local, à
`forgejo/scripts/fjbk` :

```bash
scp forgejo/scripts/fjbk forgejo/scripts/fj-check.py admin@<machine>:~/forgejo/scripts/
```

### Si on n'a besoin que du contenu d'un dépôt

Le plus court chemin, et il ne demande aucune infrastructure : le miroir.

```bash
git clone https://github.com/<org>/<dépôt>.git
```

Le miroir porte **les objets git, et rien d'autre** : ni tickets, ni demandes
d'ajout, ni comptes, ni clés. Pour ArgoCD, c'est exactement ce qu'il faut.

### Ce qu'il ne faut surtout pas faire

- **`git pull` dans la VM.** C'est ce qui referme la boucle : on demande à
  Forgejo, qui est mort, de fournir de quoi réparer Forgejo.
- **Regénérer les secrets** parce que le `.env` n'est pas sous la main. Les
  chercher vraiment d'abord : sops sur le poste, la copie de sauvegarde du
  fichier chiffré, un autre poste. Un `SECRET_KEY` neuf rend illisibles les
  jetons de miroir de la base qu'on restaurera ensuite.
