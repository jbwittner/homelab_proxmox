# Exercice de PRA — CT Forgejo

Jouer le [PRA](PRA.md) pour de faux, et **mesurer le RTO**. Tant que cet
exercice n'a pas été joué, le RTO est inconnu et le plan n'est pas prouvé — une
durée estimée de tête n'a aucune valeur le jour où on en a besoin.

**Ce que cet exercice éprouve, et qu'aucun autre ne peut éprouver :
l'APPARIEMENT.** Forgejo est en deux morceaux sauvegardés séparément — la base
par le CT 200 à 02:30, les dépôts par `vzdump` à une autre heure. Reconstruire
une instance à partir des deux, et regarder ce que l'écart produit, est la
seule chose que ce plan a de délicat.

L'exercice reconstruit une instance **dans un conteneur jetable**, à partir de
ce qui existe vraiment. Il ne touche jamais au CT 400 ni au CT 200.

---

## Garde-fous — à lire avant de commencer

Ce qui, joué distraitement, casserait la production :

- [ ] **Le CT d'exercice porte un CTID du tier jetable : `199`.** Jamais 400,
      jamais 200, jamais un CTID libre « qui traîne » — un `pct destroy` de fin
      d'exercice sur le mauvais numéro est irréversible.
- [ ] **Aucune commande de cet exercice n'écrit sur le CT 200.** On y LIT une
      sauvegarde, c'est tout. Surtout pas de `pg restore` : il écraserait la
      base de production.
- [ ] **Ne jamais jouer `fj deploy` sans `--ctid 199`.** Sans le drapeau, il
      vise le conteneur consigné dans `/etc/default/fjbk`, c'est-à-dire la
      **production**.
- [ ] **Ne pas régénérer les secrets de production.** L'exercice génère les
      siens, dans son conteneur : `fj deploy --secrets --ctid 199`.
- [ ] **Le CT d'exercice a besoin de SON PROPRE locataire de base.** Ne pas lui
      donner le mot de passe de `forgejo` : il écrirait dans la base de
      production. On crée `forgejo_pra` — voir la préparation.
- [ ] **Ne pas router `forgejo.lan.wittner.tech` vers le CT d'exercice.**
      L'exercice se joue en visant l'IP directement.

Si l'un de ces points n'est pas clair au moment de taper : **s'arrêter**.

---

## Fiche d'exercice

| Champ | Valeur |
|---|---|
| Date | ____________________ |
| Joué par | ____________________ |
| Scénario rejoué | ☐ 3 conteneur détruit ☐ 4 nœud perdu |
| Instantané de base utilisé | ____________________ |
| vzdump utilisé (le cas échéant) | ____________________ |
| Version Forgejo posée | ____________________ |
| **Début** (première commande tapée) | ____:____ |
| **Fin** (première vérification verte) | ____:____ |
| **RTO mesuré** | ________ min |
| Interruptions / imprévus | ____________________ |

---

## Préparation

- [ ] Relever un instantané récent **sur le nœud** :
      ```bash
      pg list
      pg show 20260821-023000        # les fichiers, dont forgejo.dump
      ```
      Instantané retenu : ____________________
- [ ] Vérifier que les secrets sont dans OpenBao — **avant** d'en avoir
      besoin :
      ```bash
      bao kv get homelab/forgejo
      ```
      Les quatre présents ? ☐ oui ☐ non → **si non, l'exercice s'arrête ici**,
      et c'est déjà le résultat le plus utile qu'il pouvait produire.
- [ ] **Créer un locataire d'exercice** sur le CT 200, distinct de la
      production :
      ```bash
      pg deploy --tenant forgejo_pra
      ```
      Mot de passe noté ? ☐ oui — il servira à l'étape 3, et il est jetable.
- [ ] Ajouter sa ligne dans le `pg_hba.conf` du CT 200, **avant le `reject`** :
      ```
      hostssl   forgejo_pra   forgejo_pra   192.168.1.199/32   scram-sha-256
      ```
      puis `pg deploy`.
- [ ] Noter l'heure de début : ____:____

---

## Déroulé

### 1. Créer le conteneur d'exercice

- [ ] ```bash
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
      pct exec 199 -- apt-get install -y sudo
      ```
- [ ] `--onboot 0` : un conteneur d'exercice ne doit pas revenir tout seul
      après un redémarrage du nœud.

### 2. Charger la base d'exercice

- [ ] Restaurer le dump de production **dans le locataire d'exercice**, sur le
      CT 200 — c'est la seule écriture autorisée, et elle ne touche pas
      `forgejo` :
      ```bash
      SNAP=<instantané>
      pct exec 200 -- sudo -u postgres pg_restore \
           -d forgejo_pra --no-owner --role=forgejo_pra \
           /var/backups/postgresql/$SNAP/forgejo.dump
      ```
- [ ] Vérifier qu'on n'a pas touché la production :
      ```bash
      pg verify forgejo          # doit être inchangée
      ```

### 3. Déployer

- [ ] Déposer le mot de passe du locataire d'exercice :
      ```bash
      pct exec 199 -- install -d -m 700 -o root -g root /etc/forgejo/secrets
      printf '%s' '<mot de passe forgejo_pra>' \
        | pct exec 199 -- sh -c 'umask 027 && cat > /etc/forgejo/secrets/db_password'
      ```
- [ ] ```bash
      cd /root/homelab_proxmox
      pve-eranikus/forgejo/fj deploy --ctid 199 --secrets
      ```
      *(Le déploiement visera la base `forgejo`, pas `forgejo_pra` : c'est
      attendu, et c'est ce qui rend l'étape suivante nécessaire.)*
- [ ] Corriger `app.ini` du CT 199 pour viser `forgejo_pra`, puis redémarrer.
      **Noter combien de temps ça prend** : c'est un frottement réel du plan,
      et si c'est pénible ici, ce le sera aussi le jour J. ________ min
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

### 4. Noter l'heure de fin

- [ ] Première vérification verte à ____:____ → **RTO = ________ min**

---

## Ce qu'on vérifie ensuite

Un exercice qui s'arrête à « le service démarre » ne prouve pas grand-chose.

- [ ] `fj deploy --ctid 199 --status` : le bilan est-il vert ?
- [ ] `pct exec 199 -- /opt/forgejo/forgejo --version` → ____________
      Correspond à `ct/VERSION` ? ☐ oui ☐ non
- [ ] `pct exec 199 -- git config --system --list | grep fsck` → trois lignes ?
      ☐ oui ☐ non
- [ ] Ouvrir `http://192.168.1.199:3000/` : l'interface répond ? ☐ oui ☐ non
- [ ] **Se connecter avec un compte réel** de l'instantané. ☐ oui ☐ non
      *(Le mot de passe est haché indépendamment de `secret_key` : il doit
      marcher. Si la 2FA est active sur ce compte, elle ne marchera PAS — les
      secrets 2FA sont chiffrés par le `secret_key` de production, et
      l'exercice en a généré un nouveau. C'est le comportement attendu, et
      c'est exactement ce que le [scénario 5](PRA.md#5--les-secrets-sont-perdus)
      décrit.)*

### L'appariement — le cœur de cet exercice

- [ ] Combien de dépôts la base annonce-t-elle ? ________
- [ ] Combien existent sur le disque du CT 199 ? ________
      ```bash
      pct exec 199 -- find /var/lib/forgejo/repositories -maxdepth 2 -name '*.git' | wc -l
      ```
- [ ] **L'écart est-il celui attendu ?** ☐ oui ☐ non
      *(Sans restauration de vzdump, le disque est VIDE et la base en annonce
      des dizaines. Un clone échouera donc, et c'est NORMAL — c'est la
      démonstration que la base seule ne suffit pas. Noter le message
      obtenu : ________________________)*
- [ ] **Variante complète** : restaurer aussi un `vzdump` du CT 400 dans le
      CT 199 avant l'étape 3, et refaire ce comptage. C'est le seul moyen de
      mesurer l'écart réel entre les deux sauvegardes.
      Jouée ? ☐ oui ☐ non — écart constaté : ________ dépôts

---

## Ce que cet exercice ne prouve pas

À écrire noir sur blanc, pour qu'un exercice réussi ne se confonde pas avec
« on est couvert » :

- **Il ne teste pas le chemin Traefik** : ni HTTPS, ni le routeur TCP SSH.
- **Il ne teste pas la récupération depuis GCS** : le dump vient du CT 200, pas
  du bucket. Voir la variante ci-dessous.
- **Il ne teste pas la perte du CT 200.** Celle-là est dans
  [le PRA du CT 200](../../pgsql/doc/PRA.md), et elle a son propre exercice.

### Variante : partir de GCS

Plus proche du [scénario 4](PRA.md#4--le-nœud-est-perdu), et la seule qui
prouve que le hors-site sert à quelque chose. Remplacer l'étape 2 par une
récupération depuis le bucket — la procédure est dans
[le runbook du CT 200](../../pgsql/doc/RUNBOOK.md#restauration-depuis-gcs).

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
      pct list          # 199 ne doit plus apparaître, 400 et 200 doivent être là
      ```
- [ ] **Le locataire d'exercice est retiré du CT 200** :
      ```bash
      pct exec 200 -- sudo -u postgres dropdb forgejo_pra
      pct exec 200 -- sudo -u postgres dropuser forgejo_pra
      ```
      *(Le laisser en place ferait grossir chaque sauvegarde du CT 200 d'une
      copie complète de Forgejo, indéfiniment.)*
- [ ] **Sa ligne est retirée du `pg_hba.conf`** du CT 200, puis `pg deploy`.
- [ ] `/etc/default/fjbk` porte toujours `FJ_CTID=400` :
      ```bash
      cat /etc/default/fjbk
      ```
      *(L'exercice utilise `--ctid`, qui ne consigne rien. Le vérifier quand
      même : c'est le fichier qui décide de la cible de toutes les commandes
      suivantes.)*
- [ ] La production est intacte :
      ```bash
      fj status
      pg status
      pg verify forgejo
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
