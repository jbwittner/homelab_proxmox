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

---

## 1 — Forgejo indisponible, VM saine

La VM répond en SSH, `https://forgejo.wittner.tech/` non.

### Constater

```bash
ssh admin@192.168.1.56
cd /opt/homelab/forgejo
./scripts/fj-check.py
```

Cinq lignes sortent, une par contrôle. La première qui porte `KO` désigne la
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
scp /tmp/.env admin@192.168.1.56:/opt/homelab/forgejo/.env
shred -u /tmp/.env
# Dans la VM
docker compose up -d
```

### Cas D — le disque est plein

```bash
df -h /srv /
sudo /opt/homelab/forgejo/scripts/fjbk list
```

Purger d'anciennes paires locales (le distant n'est jamais touché par ce code) :

```bash
sudo rm /srv/forgejo/backups/db-<vieil-horodatage>.dump
sudo rm /srv/forgejo/backups/data-<vieil-horodatage>.tar.gz
```

### Cas E — les données sont corrompues

Passer à une restauration de paire, la pile étant saine :

```bash
sudo /opt/homelab/forgejo/scripts/fjbk list
sudo /opt/homelab/forgejo/scripts/fjbk verify <horodatage>
sudo /opt/homelab/forgejo/scripts/fjbk restore <horodatage>
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
cd /opt/homelab/forgejo
./scripts/fj-check.py
```

Le vzdump porte la VM **entière**, disque de données compris : la base et les
dépôts sont cohérents entre eux, il n'y a pas de second temps. Si le vzdump est
plus vieux que la dernière paire de sauvegarde, rattraper avec :

```bash
sudo /opt/homelab/forgejo/scripts/fjbk list
sudo /opt/homelab/forgejo/scripts/fjbk restore <horodatage-plus-récent>
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
  --cores 2 --sockets 1 --cpu host \
  --memory 4096 \
  --net0 virtio,bridge=vmbr0 \
  --scsihw virtio-scsi-single \
  --ostype l26 \
  --serial0 socket --vga serial0 \
  --agent enabled=1 \
  --onboot 1 \
  --startup order=1

qm disk import 300 /var/lib/vz/template/iso/debian-13-genericcloud-amd64.qcow2 local-lvm
qm set 300 --scsi0 local-lvm:vm-300-disk-0
qm disk resize 300 scsi0 20G
qm set 300 --scsi1 data:40

qm set 300 --ide2 local-lvm:cloudinit
qm set 300 --boot order=scsi0
qm set 300 --ciuser admin
qm set 300 --sshkeys /root/.ssh/forgejo-admin.pub
qm set 300 --ipconfig0 ip=192.168.1.56/24,gw=192.168.1.254
qm set 300 --nameserver 192.168.1.254
qm set 300 --ciupgrade 0

qm start 300
```

### 3.3 — Formater `/srv`

> **DESTRUCTIF.** Vérifier `lsblk` avant : le disque visé est le second, celui
> de 40 Go, et il doit être vide.

```bash
ssh admin@192.168.1.56
lsblk
sudo mkfs.ext4 -L srv /dev/sdb
sudo blkid -L srv        # doit répondre /dev/sdb
```

### 3.4 — Poser le dépôt et provisionner

**Le clone se fait depuis GitHub, pas depuis Forgejo** — Forgejo est justement
ce qu'on est en train de reconstruire.

```bash
sudo mkdir -p /opt/homelab
sudo chown admin:admin /opt/homelab
git clone https://github.com/<org>/homelab_proxmox.git /opt/homelab

sudo /opt/homelab/forgejo/scripts/init.sh
```

`init.sh` installe Docker, monte `/srv` par étiquette, pose rclone. Il refuse de
tourner si `blkid -L srv` ne répond rien — c'est-à-dire si l'étape 3.3 a été
sautée.

Se déconnecter et se reconnecter pour que l'appartenance au groupe `docker`
prenne effet.

### 3.5 — Reposer les secrets

```bash
# Sur le POSTE
sops -d forgejo.env.sops > /tmp/.env
scp /tmp/.env admin@192.168.1.56:/opt/homelab/forgejo/.env
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
cd /opt/homelab/forgejo
docker compose pull
docker compose up -d
docker compose ps          # attendre que db soit « healthy »
```

### 3.7 — Rapatrier et restaurer

```bash
sudo /opt/homelab/forgejo/scripts/fjbk list
# les paires distantes s'affichent avec « complet » ou « INCOMPLET »

sudo /opt/homelab/forgejo/scripts/fjbk restore <horodatage>
```

`restore` rapatrie ce qui manque localement, arrête Forgejo, recrée la base,
rejoue le dump, extrait les dépôts, repose les propriétaires en `1000:1000`,
redémarre la pile et appelle `fj-check.py`. Il **demande de retaper
l'horodatage** avant d'écraser quoi que ce soit.

Si le rapatriement est long et qu'on préfère éprouver la paire d'abord :

```bash
sudo /opt/homelab/forgejo/scripts/fjbk verify <horodatage>
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
sudo install -m 0644 /opt/homelab/forgejo/scripts/fjbk.service \
                     /opt/homelab/forgejo/scripts/fjbk.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fjbk.timer
sudo systemctl list-timers fjbk.timer
```

### 3.10 — Recette

```bash
cd /opt/homelab/forgejo && ./scripts/fj-check.py
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
