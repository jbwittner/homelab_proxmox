# Exercice de PRA — scénario « nœud perdu »

Jouer [le scénario 3 du PRA](PRA.md#3--nœud-perdu--sinistre) **pour de faux**,
sur une VM jetable, et **mesurer le RTO**. Tant que cet exercice n'a pas été
joué, le RTO du PRA reste vide — et une durée estimée de tête n'a aucune valeur
le jour où on en a besoin.

C'est **la seule épreuve** de `fjbk` et de `fj-check.py`. Ce dépôt n'a pas de
tests unitaires sur ses scripts, délibérément : ce qu'on veut savoir n'est pas
si une fonction se comporte comme sa spécification, c'est si la reprise
fonctionne et combien de temps elle prend.

## Garde-fous — à lire avant de commencer

Ce qui, joué distraitement, casserait la production :

- [ ] **La VM d'exercice porte un VMID de la plage jetable, 100–199.** Jamais
      300. Un `qmrestore … 300 --force 1` tapé de travers détruit la vraie.
- [ ] **Elle ne prend PAS l'adresse `192.168.1.56`.** Deux machines sur la même
      IP, et Traefik route au hasard vers l'une ou l'autre — y compris pour les
      pushes des utilisateurs.
- [ ] **Aucun `docker compose down -v`** sur quoi que ce soit qui ne soit pas la
      VM d'exercice.
- [ ] **Ne jamais lancer `fjbk backup` sur la VM d'exercice** avec le vrai
      bucket : le compte de service ne peut pas écraser, mais il peut **créer**.
      Des paires d'exercice se mélangeraient aux vraies dans le bucket.
      → n'armer ni `fjbk.timer`, ni `fjbk.service`.
- [ ] **La restauration ne touche que la VM d'exercice.** `fjbk restore` lit le
      bucket, il n'y écrit rien.
- [ ] Le miroir push n'est **pas** configuré sur l'instance d'exercice : elle
      pousserait vers le vrai GitHub.

## Ce que l'exercice ne prouve pas

- Que le **nœud** de repli existe le jour venu. L'exercice se joue sur le nœud
  courant, pas sur un nœud de secours qui n'a jamais démarré.
- Que les **secrets** sont récupérables. Ils viennent de sops, et si sops
  fonctionne pendant l'exercice, c'est parce que le poste va bien.
- Que **GCS** est joignable depuis ailleurs que le LAN.
- Que le RTO mesuré vaut pour un vrai sinistre : ici, on ne cherche pas le
  matériel, on ne prévient personne, et on sait déjà quoi faire.

## Préparation

| | |
|---|---|
| Date de l'exercice | `____________` |
| Qui | `____________` |
| VMID jetable utilisé | `____________` (plage 100–199) |
| IP de la VM d'exercice | `____________` |
| Horodatage de paire visé | `____________` |
| Taille de la paire | `____________` |

```bash
# Relever la paire visée AVANT de commencer, depuis la vraie VM
sudo /opt/homelab/forgejo/scripts/fjbk list
```

## Le chronomètre

**Il démarre au moment où on décide que le nœud est perdu**, pas quand la
première commande est tapée. Le temps de lire le PRA fait partie du RTO.

| | Heure | Cumul |
|---|---|---|
| **T0 — décision** | `______` | — |
| Image Debian récupérée et vérifiée (§ 3.1) | `______` | `______` |
| VM créée et démarrée (§ 3.2) | `______` | `______` |
| `/srv` formaté et étiqueté (§ 3.3) | `______` | `______` |
| `init.sh` terminé (§ 3.4) | `______` | `______` |
| Secrets et clé rclone reposés (§ 3.5) | `______` | `______` |
| Pile vide démarrée, `db` healthy (§ 3.6) | `______` | `______` |
| Paire rapatriée (§ 3.7) | `______` | `______` |
| `fjbk restore` terminé (§ 3.7) | `______` | `______` |
| `fj-check.py` au vert (§ 3.10) | `______` | `______` |
| **Clone HTTPS réussi depuis le LAN** | `______` | `______` |
| **Clone SSH réussi depuis le LAN** | `______` | `______` |

> **RTO mesuré : `____________`**
>
> À reporter dans [PRA.md](PRA.md#ce-quon-perd-et-ce-quon-ne-perd-pas).

## Déroulé

Suivre [PRA § 3](PRA.md#3--nœud-perdu--sinistre) **sans le paraphraser ici** :
si une commande manque là-bas, c'est le PRA qu'il faut corriger, pas cet
exercice qu'il faut compléter.

- [ ] § 3.1 — image récupérée, `sha512sum` **vérifié** (ne pas sauter : c'est
      aussi ce qu'on éprouve)
- [ ] § 3.2 — VM créée avec le VMID et l'IP **jetables** relevés plus haut
- [ ] § 3.3 — `/srv` formaté, `blkid -L srv` répond
- [ ] § 3.4 — dépôt cloné **depuis GitHub**, `init.sh` passé
- [ ] § 3.5 — `.env` et clé rclone déposés depuis le poste
- [ ] § 3.6 — `docker compose up -d`, `db` healthy
- [ ] § 3.7 — `fjbk verify <horodatage>` **avant** `fjbk restore`
- [ ] § 3.7 — `fjbk restore <horodatage>`, horodatage retapé à la confirmation
- [ ] § 3.10 — `fj-check.py` : les cinq contrôles
- [ ] § 3.10 — clone HTTPS **et** SSH, depuis une machine du LAN
- [ ] Ouvrir l'interface : un dépôt, un ticket, un compte — les trois sont là ?

### Contrôles de fond, une fois remonté

```bash
# La base a bien été rejouée dans le bon volume
docker compose exec -T db psql -U forgejo -tAc 'SHOW data_directory'
# attendu : /var/lib/postgresql/data

# Les dépôts appartiennent à l'UID du conteneur
stat -c '%u:%g %n' /srv/forgejo/data
# attendu : 1000:1000

# Forgejo est bien à la version épinglée
docker compose exec -T forgejo forgejo --version
```

- [ ] `data_directory` = `/var/lib/postgresql/data`
- [ ] `/srv/forgejo/data` en `1000:1000`
- [ ] version = celle de `compose.yaml`
- [ ] nombre de dépôts vus dans l'interface : `______` (attendu : `______`)
- [ ] nombre de comptes : `______` (attendu : `______`)

## Journal

Ce qui a coincé, ce qui manquait dans le PRA, ce qui a pris plus de temps que
prévu. **Une commande du PRA qui n'a pas marché telle quelle est un défaut du
PRA** — la corriger dans la foulée, pas « plus tard ».

```
















```

## Démontage — aucune étape n'est facultative

- [ ] `docker compose down` sur la VM d'exercice
- [ ] **Le `.env` d'exercice est détruit** :
      `shred -u ~/forgejo/.env` — il porte les vrais secrets
- [ ] **La clé du compte de service est détruite** :
      `sudo shred -u /root/.config/rclone/forgejo-backups.json`
- [ ] La VM jetable est **détruite**, pas juste éteinte :
      `qm stop <vmid> && qm destroy <vmid> --purge`
- [ ] Vérifier qu'aucune paire d'exercice n'a été créée dans le bucket :
      `rclone --config … lsjson gcs:<bucket>/forgejo | grep <date-du-jour>`
      → doit être vide
- [ ] Vérifier que la **vraie** VM va bien : `fj-check.py`, et que le timer de
      sauvegarde n'a pas été touché : `systemctl list-timers fjbk.timer`
- [ ] Le RTO mesuré est reporté dans [PRA.md](PRA.md)
- [ ] Les corrections du PRA sont commitées

## Historique des exercices

| Date | Par | RTO mesuré | Ce qui a été corrigé |
|---|---|---|---|
| | | | |
