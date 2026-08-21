# Exercice de PRA — CT Forgejo

Jouer le [PRA](PRA.md) pour de faux, et **mesurer le RTO**. Tant que cet
exercice n'a pas été joué, le RTO est inconnu et le plan n'est pas prouvé — une
durée estimée de tête n'a aucune valeur le jour où on en a besoin.

L'exercice reconstruit une instance **dans un conteneur jetable**, à partir de
ce qui existe vraiment : le dépôt, une sauvegarde, et OpenBao. Il ne touche
jamais au CT 400.

---

## Garde-fous — à lire avant de commencer

Ce qui, joué distraitement, casserait la production :

- [ ] **Le CT d'exercice porte un CTID du tier jetable : `199`.** Jamais 400,
      jamais un CTID libre « qui traîne » — un `pct destroy` de fin d'exercice
      sur le mauvais numéro est irréversible.
- [ ] **Aucune commande de cet exercice ne vise 400.** Les seules lectures
      autorisées sur le CT 400 sont `fj list` et une copie de dump.
- [ ] **Ne jamais jouer `fj deploy` sans `--ctid 199`** pendant l'exercice.
      Sans le drapeau, il vise le conteneur consigné dans `/etc/default/fjbk`,
      c'est-à-dire la **production**.
- [ ] **Ne pas armer le hors-site sur le CT d'exercice** (`--no-offsite`). Le
      compte de service ne peut pas supprimer : un instantané d'exercice déposé
      dans le bucket y resterait pour toujours.
- [ ] **Ne pas régénérer les secrets de production.** L'exercice génère les
      siens, dans son conteneur ; `fj deploy --secrets --ctid 199`.
- [ ] **Ne pas router `forgejo.lan.wittner.tech` vers le CT d'exercice.**
      L'exercice se joue en visant l'IP directement.

Si l'un de ces points n'est pas clair au moment de taper : **s'arrêter**.

---

## Fiche d'exercice

| Champ | Valeur |
|---|---|
| Date | ____________________ |
| Joué par | ____________________ |
| Scénario rejoué | ☐ 1 base perdue ☐ 3 conteneur détruit ☐ 4 nœud perdu |
| Instantané utilisé | ____________________ |
| Version Forgejo posée | ____________________ |
| **Début** (première commande tapée) | ____:____ |
| **Fin** (première vérification verte) | ____:____ |
| **RTO mesuré** | ________ min |
| Interruptions / imprévus | ____________________ |

---

## Préparation

- [ ] Relever un instantané récent et **lire son manifeste** :
      ```bash
      fj list
      pct exec 400 -- cat /var/backups/forgejo/<stamp>/MANIFEST
      ```
      `FORGEJO_VERSION` : ____________  `REPOS_COUNT` : ________
- [ ] Copier le dump hors du CT de production, **en lecture seule** :
      ```bash
      mkdir -p /tmp/pra-forgejo
      pct pull 400 /var/backups/forgejo/<stamp>/forgejo.dump \
           /tmp/pra-forgejo/forgejo.dump
      ```
- [ ] Vérifier que les secrets sont bien dans OpenBao — **avant** d'en avoir
      besoin :
      ```bash
      bao kv get homelab/forgejo
      ```
      Les quatre présents ? ☐ oui ☐ non → **si non, l'exercice s'arrête ici**,
      et c'est déjà le résultat le plus utile qu'il pouvait produire.
- [ ] Noter l'heure de début : ____:____

---

## Déroulé

### 1. Créer le conteneur d'exercice

- [ ] Créer le CT 199, en reprenant [runbook § 1](RUNBOOK.md#1-création-du-conteneur)
      avec ces différences :
      ```bash
      pct create 199 local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst \
          --hostname forgejo-pra \
          --unprivileged 1 --features nesting=1 \
          --cores 2 --memory 2048 --swap 512 \
          --rootfs local-lvm:16 \
          --net0 name=eth0,bridge=vmbr0,ip=192.168.1.199/24,gw=192.168.1.254 \
          --nameserver 192.168.1.2 \
          --onboot 0 \
          --description 'EXERCICE DE PRA — jetable. Détruire après.'
      pct start 199
      pct exec 199 -- apt-get update
      pct exec 199 -- apt-get install -y python3-minimal sudo
      ```
- [ ] `--onboot 0` : un conteneur d'exercice ne doit pas revenir tout seul
      après un redémarrage du nœud.

### 2. Déployer

- [ ] ```bash
      cd /root/homelab_proxmox
      pve-eranikus/forgejo/fj deploy --ctid 199 --no-offsite --secrets
      ```
- [ ] Le bilan est-il exploitable ? Noter **chaque ligne KO** et si elle était
      attendue :

      | Étape | Verdict | Attendu ? |
      |---|---|---|
      | | | |
      | | | |

- [ ] Combien de fois a-t-il fallu **sortir de la procédure** pour deviner
      quelque chose ? ________
      *(C'est la mesure qui compte le plus : chaque sortie est un trou dans la
      documentation, à combler AVANT le prochain exercice.)*

### 3. Restaurer la base

- [ ] ```bash
      pct push 199 /tmp/pra-forgejo/forgejo.dump /tmp/forgejo.dump
      pct exec 199 -- systemctl stop forgejo
      pct exec 199 -- sudo -u postgres dropdb forgejo
      pct exec 199 -- sudo -u postgres createdb forgejo -O forgejo -T template0 \
           --encoding UTF8 --lc-collate C --lc-ctype C
      pct exec 199 -- sudo -u postgres pg_restore -d forgejo --no-owner \
           --role=forgejo /tmp/forgejo.dump
      ```
- [ ] **Réappliquer les ACL** — l'étape qu'on saute quand on va vite :
      ```bash
      pct exec 199 -- sudo -u postgres psql -v ON_ERROR_STOP=1 \
           -f /etc/forgejo-git/init.sql
      pct exec 199 -- sudo -u postgres psql -c '\l forgejo'
      ```
      « Access privileges » non vide ? ☐ oui ☐ non
- [ ] ```bash
      pct exec 199 -- systemctl start forgejo
      ```

### 4. Noter l'heure de fin

- [ ] Première vérification verte à ____:____ → **RTO = ________ min**

---

## Ce qu'on vérifie ensuite

Un exercice qui s'arrête à « le service démarre » ne prouve pas grand-chose.

- [ ] `fj deploy --ctid 199 --status --no-offsite` : le bilan est-il vert
      hormis ce qui est légitimement absent (hors-site) ?
- [ ] `pct exec 199 -- /opt/forgejo/forgejo --version` → version : ____________
      Correspond-elle à `ct/VERSION` ? ☐ oui ☐ non
- [ ] `pct exec 199 -- ss -lntp` → **aucune** socket sur 5432 ? ☐ oui ☐ non
- [ ] `pct exec 199 -- git config --system --list | grep fsck` → trois lignes ?
      ☐ oui ☐ non
- [ ] Ouvrir `http://192.168.1.199:3000/` : l'interface répond ? ☐ oui ☐ non
- [ ] **Se connecter avec un compte réel** de l'instantané. ☐ oui ☐ non
      *(Le mot de passe est haché indépendamment de `secret_key` : il doit
      marcher. Si la 2FA est active sur ce compte, elle ne marchera PAS —
      les secrets 2FA sont chiffrés par le `secret_key` de production, et
      l'exercice en a généré un nouveau. C'est le comportement attendu, et
      c'est exactement ce que le [scénario 5](PRA.md#5--les-secrets-sont-perdus)
      décrit.)*
- [ ] **Cloner un dépôt** depuis l'instance d'exercice :
      ```bash
      git clone http://192.168.1.199:3000/<org>/<dépôt>.git /tmp/pra-clone
      ```
      Le clone aboutit ? ☐ oui ☐ non
      *(La base connaît le dépôt ; le disque, lui, est vide — l'exercice n'a
      pas restauré `/var/lib/forgejo`. Un clone qui échoue ici est donc
      NORMAL, et c'est précisément la démonstration que **la base seule ne
      suffit pas**. Noter le message obtenu : ________________________)*
- [ ] Deux ou trois dépôts sont-ils listés dans l'interface, avec leurs
      tickets ? ☐ oui ☐ non

---

## Ce que cet exercice ne prouve pas

À écrire noir sur blanc, pour qu'un exercice réussi ne se confonde pas avec
« on est couvert » :

- **Il ne restaure pas les dépôts.** Ils viennent du `vzdump`, pas de la
  sauvegarde logique. Un exercice complet demanderait un `pct restore` d'un
  vzdump du CT 400 — plus long, plus lourd, et à jouer séparément.
- **Il ne teste pas le chemin Traefik** : ni HTTPS, ni le routeur TCP SSH.
- **Il ne teste pas la récupération depuis GCS** : le dump vient du CT de
  production, pas du bucket. Voir la variante ci-dessous.

### Variante : partir de GCS

Plus proche du [scénario 4](PRA.md#4--le-nœud-est-perdu), et la seule qui
prouve que le hors-site sert à quelque chose. Remplacer l'étape « Préparation »
par :

```bash
rclone --config /root/.config/rclone/rclone.conf --gcs-bucket-policy-only \
  lsf gcs:homelab-pgsql-backups-dc93212a/pve-eranikus/forgejo/
rclone --config /root/.config/rclone/rclone.conf --gcs-bucket-policy-only \
  copy gcs:homelab-pgsql-backups-dc93212a/pve-eranikus/forgejo/<stamp>/ \
  /tmp/pra-forgejo/
```

- [ ] Variante GCS jouée ? ☐ oui ☐ non — durée du transfert : ________ min

---

## Démontage — aucune étape n'est facultative

- [ ] Le CT 199 est arrêté :
      ```bash
      pct stop 199
      ```
- [ ] Le CT 199 est détruit — **vérifier le numéro deux fois** :
      ```bash
      pct destroy 199 --purge
      pct list          # 199 ne doit plus apparaître, 400 doit être là
      ```
- [ ] Les fichiers temporaires sont retirés du nœud :
      ```bash
      rm -rf /tmp/pra-forgejo /tmp/pra-clone
      ```
- [ ] `/etc/default/fjbk` porte toujours `FJ_CTID=400` :
      ```bash
      cat /etc/default/fjbk
      ```
      *(L'exercice utilise `--ctid`, qui ne consigne rien. Le vérifier quand
      même : c'est le fichier qui décide de la cible de toutes les commandes
      suivantes, et une erreur ici viserait la production sans le dire.)*
- [ ] La production est intacte :
      ```bash
      fj status
      ```
- [ ] Aucun instantané d'exercice n'est parti hors-site :
      ```bash
      fj offsite --dry-run
      ```

---

## Journal

Ce qui a bloqué, ce qui a manqué, ce qui était faux dans la documentation.
**Chaque ligne ici est une correction à faire dans le runbook ou le PRA avant
le prochain exercice.**

| # | Ce qui s'est passé | Où corriger |
|---|---|---|
| 1 | | |
| 2 | | |
| 3 | | |

- [ ] Les corrections ci-dessus sont commitées.
- [ ] Le **RTO mesuré** est reporté dans [PRA.md](PRA.md#ce-quon-perd-et-ce-quon-ne-perd-pas).
