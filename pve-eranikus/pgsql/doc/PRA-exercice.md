# Exercice de PRA — simulation complète

Un PRA qui n'a jamais été joué n'est pas un plan, c'est une intention. Ce
document est la procédure de **test** du [PRA](PRA.md) : ce qu'on joue, ce
qu'on mesure, et ce qu'on note.

**L'exercice sert à trouver ce qui manque.** Une anomalie trouvée ici est un
succès, pas un échec — c'est exactement ce pour quoi on le joue.

## Deux niveaux

| | Exercice court | Exercice complet | Exercice de bascule |
|---|---|---|---|
| Ce qu'il prouve | la chaîne de sauvegarde est **fidèle et restaurable** | le [scénario 4](PRA.md#4--le-nœud-est-perdu) fonctionne de bout en bout | `pg restore` fait ce que faisait `pgbk restore` |
| Où | dans le CT 200, sur une base jetable | sur un CT jetable, CTID **299** | dans le CT 200, locataire et dépôt jetables |
| Impact sur la production | aucun | aucun, si les deux garde-fous ci-dessous sont respectés | aucun, si `PG_BACKUP_DEST` est bien détourné |
| Durée indicative | ~30 min | ~2 h | ~30 min |
| Cadence | trimestrielle | semestrielle, **et après tout changement structurel** | **une fois**, avant de retirer le moteur bash |

## Les deux garde-fous

À lire **avant** de taper quoi que ce soit. Les deux failles viennent du même
endroit : `pg deploy` a été écrit pour déployer, pas pour jouer.

> **1. Toujours `--no-offsite` sur le CT d'exercice.**
>
> Sans ce drapeau, `pg deploy` réécrit le drop-in de `pgbk-offsite` avec le
> volume du CT visé. La copie hors-site de production partirait dès 3h30 sur
> les sauvegardes du CT d'exercice, et ces objets-là **ne pourront jamais être
> supprimés depuis le nœud**.

> **2. Remettre le CTID de production à la fin.**
>
> `pg deploy --ctid 299` consigne `PG_CTID=299` dans `/etc/default/pgbk` :
> tous les `pgbk` suivants viseraient le CT d'exercice, y compris un
> `pgbk restore` fait en urgence trois semaines plus tard. Le démontage le
> remet — ne pas sauter cette étape.

Troisième règle, qui ne relève pas d'un drapeau : **l'exercice ne lit que le
bucket, il n'y écrit rien.** La récupération se fait avec le compte personnel
(`gcloud`), jamais avec la clé du nœud.

## Exercice court

Prouve que ce qui est parti dans GCS est bien ce qui était sur le disque, et
que ça se recharge. Se joue sur le CT 200 sans y toucher.

### 1. Choisir et récupérer

```bash
# sur le nœud : quel instantané est aussi en ligne ?
pgbk list
rclone --config /root/.config/rclone/rclone.conf \
       lsf gcs:homelab-pgsql-backups-dc93212a/pve-eranikus/postgresql/

# depuis le poste d'administration, avec le COMPTE PERSONNEL
gcloud auth login
gcloud storage cp -r \
  gs://homelab-pgsql-backups-dc93212a/pve-eranikus/postgresql/<instantané> .
cat <instantané>/MANIFEST
```

- [ ] Le `MANIFEST` porte la bonne date, la bonne version PostgreSQL, et la
      liste des bases attendue.

### 2. Comparer au local — la copie est-elle fidèle ?

```bash
# empreintes de la copie récupérée
md5sum <instantané>/*

# empreintes de l'original, sur le nœud. La vue HÔTE du dataset est écrite
# dans le drop-in par pg deploy : la lire plutôt que la recopier de mémoire.
SRC=$(sed -n 's/^Environment=PGBK_OFFSITE_SRC=//p' \
      /etc/systemd/system/pgbk-offsite.service.d/10-noeud.conf)
md5sum "$SRC"/<instantané>/*
```

- [ ] **Les empreintes sont identiques**, fichier par fichier. C'est le
      contrôle qui prouve la chaîne hors-site ; le reste de l'exercice teste
      la restauration, pas la copie.

### 3. Restaurer dans une base jetable

```bash
pct push 200 <instantané>/forgejo.dump /tmp/pra.dump
pct enter 200

sudo -u postgres createdb forgejo_pra -O forgejo -T template0 \
     --encoding UTF8 --lc-collate C --lc-ctype C
sudo -u postgres pg_restore -d forgejo_pra --no-owner --role=forgejo /tmp/pra.dump
```

- [ ] `pg_restore` se termine sans erreur.

```bash
sudo -u postgres psql -d forgejo_pra -c "\dt"
sudo -u postgres psql -d forgejo_pra -tAc \
  "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
```

- [ ] Le nombre de tables correspond à celui de la base de production.
- [ ] Les tables appartiennent à `forgejo`, pas à `postgres` — sinon le
      `--role` a été oublié quelque part.
- [ ] Deux ou trois tables métier contiennent des lignes cohérentes avec la
      date de l'instantané.

### 4. Démonter

```bash
sudo -u postgres dropdb forgejo_pra
rm -f /tmp/pra.dump
exit
rm -rf <instantané>          # sur le poste : il contient des empreintes SCRAM
```

- [ ] La base jetable est supprimée, le dump temporaire aussi.
- [ ] **La copie locale du poste est effacée** : `globals.sql` porte les
      empreintes de tous les rôles.

## Exercice complet

Rejoue le [scénario 4](PRA.md#4--le-nœud-est-perdu) — nœud perdu — sur un
conteneur jetable. Le seul écart avec la réalité est qu'on ne rachète pas de
matériel : le CT 299 tient lieu de machine de remplacement.

### 0. État de départ

```bash
date                                          # heure de début, à noter
pve-eranikus/pgsql/pg deploy --status      # la production est saine ?
cat /etc/default/pgbk                         # doit dire PG_CTID=200
```

- [ ] La production est conforme **avant** de commencer. On ne joue pas un
      exercice par-dessus une anomalie.
- [ ] `PG_CTID=200` est noté quelque part pour le démontage.

**Début : ____h____**

### 1. Créer le conteneur d'exercice

Script communautaire ([runbook § 1](RUNBOOK.md#1-création-du-conteneur)),
CTID **299**, mêmes réponses qu'en production — **nesting = oui** — avec une IP
libre et un disque plus petit.

- [ ] CT 299 créé et démarré.

**Durée mesurée : ______**

### 2. Reposer le service

```bash
cd /root/homelab_proxmox && git pull
PG_MP2_SIZE=10 pve-eranikus/pgsql/pg deploy --ctid 299 --no-offsite
```

`PG_MP2_SIZE=10` évite d'allouer 50 Go pour un exercice. `--no-offsite` est
**obligatoire** (garde-fou 1).

- [ ] Le résumé ne porte aucun `KO` autre que ceux liés au hors-site.
- [ ] `pg deploy --ctid 299 --no-offsite --dry-run` annonce ensuite **zéro
      modification** : le script décrit bien l'état qu'il vient de poser.

**Durée mesurée : ______**

### 3. Récupérer depuis GCS, avec le compte personnel

```bash
gcloud auth login
gcloud storage cp -r \
  gs://homelab-pgsql-backups-dc93212a/pve-eranikus/postgresql/<instantané> .
cat <instantané>/MANIFEST
scp -r <instantané> root@192.168.1.11:/tmp/
```

- [ ] La récupération n'a demandé **aucun secret du nœud**. C'est le point
      central du scénario 4 : si un accès a manqué ici, le PRA est faux.

**Durée mesurée : ______**

### 4. Charger dans le CT 299

```bash
pct exec 299 -- mkdir -p /var/backups/postgresql/<instantané>
for f in /tmp/<instantané>/*; do
  pct push 299 "$f" "/var/backups/postgresql/<instantané>/$(basename "$f")"
done
pct exec 299 -- chown -R postgres:postgres /var/backups/postgresql/<instantané>
pct exec 299 -- chmod 700 /var/backups/postgresql/<instantané>

# les rôles D'ABORD : pgbk restore refuse une base dont le rôle propriétaire
# n'existe pas, et c'est le rappel que globals.sql passe en premier
pct exec 299 -- sudo -u postgres psql -f /var/backups/postgresql/<instantané>/globals.sql

pgbk --ctid 299 show    <instantané>
pgbk --ctid 299 restore forgejo <instantané>
pgbk --ctid 299 verify  forgejo
```

- [ ] `globals.sql` recrée les rôles sans erreur.
- [ ] `pgbk restore` va au bout.
- [ ] `pgbk verify` montre une ACL non vide et « propriétaire des tables : OK ».

**Durée mesurée : ______**

### 5. Contrôles fonctionnels

Le vrai test n'est pas que `pg_restore` finisse, c'est qu'un client puisse
travailler :

```bash
# nombre de tables et de lignes, à comparer à la production
pct exec 299 -- sudo -u postgres psql -d forgejo -c "\dt"
pct exec 299 -- sudo -u postgres psql -c "\l forgejo"    # ACL : doit afficher =T/forgejo

# connexion en tant que locataire, comme le ferait le service. Depuis
# « pct enter » et non « pct exec » : ce dernier n'alloue pas de TTY, psql ne
# pourrait pas demander le mot de passe.
pct enter 299
psql "postgresql://forgejo@127.0.0.1/forgejo?sslmode=require" -c "\dt"
exit
```

- [ ] Le rôle du locataire se connecte **avec son mot de passe de production**
      — preuve que `globals.sql` a rendu les empreintes SCRAM
      ([PRA scénario 6](PRA.md#6--les-secrets-sont-perdus)).
- [ ] La colonne `Access privileges` de `\l forgejo` n'est **pas vide** :
      `PUBLIC` n'a pas retrouvé `CONNECT`.
- [ ] Le contenu est cohérent avec la date de l'instantané.

**RTO total mesuré (étapes 1 à 5) : ______**

### 6. Démonter — ne rien sauter

```bash
pct stop 299
pct set 299 --protection 0        # si le script communautaire l'a posée
pct destroy 299

# GARDE-FOU 2 : remettre le CTID de production. Un VRAI passage, pas --status :
# en mode --status rien n'est écrit, et /etc/default/pgbk resterait sur 299.
pve-eranikus/pgsql/pg deploy --ctid 200
cat /etc/default/pgbk             # doit redire PG_CTID=200

# la production n'a pas bougé
pve-eranikus/pgsql/pg deploy --status
systemctl list-timers pgbk-offsite.timer
pct exec 200 -- systemctl list-timers pg-backup.timer

rm -rf <instantané> /tmp/<instantané>   # empreintes SCRAM
```

- [ ] CT 299 détruit, son volume `mp2` avec.
- [ ] `/etc/default/pgbk` dit de nouveau `PG_CTID=200`.
- [ ] Le drop-in `pgbk-offsite` pointe toujours le volume du CT **200** :
      `grep SRC /etc/systemd/system/pgbk-offsite.service.d/10-noeud.conf`.
- [ ] Les deux timers sont armés, sur l'hôte et dans le CT.
- [ ] Les copies contenant `globals.sql` sont effacées du poste et de `/tmp`.

### 7. Consigner

- [ ] Ligne ajoutée au journal ci-dessous.
- [ ] **Chaque anomalie rencontrée est corrigée dans le dépôt**, pas seulement
      notée : une étape manquante devient une ligne de `PRA.md`, un geste
      oublié devient une ligne de `pg deploy`.

## Exercice de bascule — valider le moteur Python

**Se joue une fois**, avant de retirer `pgbk` du conteneur. Les deux autres
exercices prouvent que la chaîne de sauvegarde est fidèle ; celui-ci prouve que
`pg restore` fait ce que faisait `pgbk restore`, et le prouve sur des données
qu'on peut se permettre de perdre.

| | |
|---|---|
| Ce qu'il prouve | `pg restore` restaure, rend la base à son propriétaire, et **réapplique les ACL** |
| Où | dans le CT 200, sur un locataire jetable et un dépôt de sauvegardes temporaire |
| Impact sur la production | aucun — ni la base `forgejo`, ni le dépôt `/var/backups/postgresql`, ni le bucket ne sont touchés |
| Durée indicative | ~30 min |
| Cadence | **une seule fois**, puis à chaque changement de `restore.py` |

### Le garde-fou de cet exercice

> **Toujours `PG_BACKUP_DEST` sur un répertoire temporaire.**
>
> Sans cette variable, la sauvegarde d'exercice atterrirait dans
> `/var/backups/postgresql`, deviendrait le `latest` du CT, et **partirait dans
> le bucket à 3h30** — où plus personne ne peut l'effacer. Elle contiendrait en
> outre les empreintes SCRAM de tous les rôles, pour une base de test.
>
> Le dépôt temporaire vit dans le CT, il est en `700`, et le démontage
> l'efface.

Les deux garde-fous des autres exercices ne s'appliquent pas ici : on ne crée
pas de conteneur, et on ne touche pas à `/etc/default/pgbk`.

### 1. Un locataire jetable

```bash
# sur le nœud
pve-eranikus/pgsql/pg deploy --tenant pra
```

Le mot de passe s'affiche une fois. **Il n'a pas besoin d'être conservé** : ce
locataire sera supprimé au démontage, et l'exercice se connecte en `peer`.

```bash
pct enter 200
sudo -u postgres psql -d pra -c "
  CREATE TABLE facture (id serial PRIMARY KEY, montant numeric, pose date);
  INSERT INTO facture (montant, pose)
  SELECT g * 10.5, current_date - g FROM generate_series(1, 500) g;
  ALTER TABLE facture OWNER TO pra;"
sudo -u postgres psql -d pra -tAc "SELECT count(*), sum(montant) FROM facture"
```

- [ ] La base `pra` contient **500** lignes. Noter la somme : _______________
- [ ] `pg show` n'est pas encore concerné ; on est dans le CT, en `pct enter`.

### 2. Une sauvegarde, hors du dépôt de production

```bash
mkdir -p /tmp/pra-backups && chmod 700 /tmp/pra-backups
chown postgres:postgres /tmp/pra-backups

# « sudo env VAR=… » et non « VAR=… sudo » : sudo efface l'environnement de
# l'appelant, et le détournement du dépôt serait perdu — la sauvegarde
# d'exercice atterrirait en production, puis dans le bucket.
sudo -u postgres env PG_BACKUP_DEST=/tmp/pra-backups \
  /usr/local/bin/pg-backup.sh --json > /tmp/pra.json
python3 -m json.tool /tmp/pra.json | head -20
```

- [ ] `"status": "ok"` et `"exit_code": 0`.
- [ ] `pra` figure dans `"databases"`.
- [ ] **`/var/backups/postgresql` n'a pas bougé** :
      `ls /var/backups/postgresql | wc -l` donne le même nombre qu'avant.
      Si ce nombre a augmenté, le détournement n'a pas pris : **arrêter là**,
      supprimer l'instantané fautif avec `pg delete`, et vérifier avant 3h30
      qu'il n'est pas parti dans le bucket.

### 3. Le dégât

```bash
sudo -u postgres psql -d pra -c "DELETE FROM facture WHERE id > 100"
sudo -u postgres psql -d pra -tAc "SELECT count(*) FROM facture"
```

- [ ] Il ne reste que **100** lignes. C'est le `DELETE` parti trop loin du
      [scénario 1](PRA.md#1--une-base-perdue-ou-corrompue).

### 4. Restaurer avec le moteur Python

```bash
PG_BACKUP_DEST=/tmp/pra-backups /usr/local/bin/pg restore pra --yes
echo "code de retour : $?"
```

- [ ] **Code de retour 0.**
- [ ] Le journal montre, dans cet ordre : le propriétaire capturé, le filet
      `pre-restore-*`, les sessions fermées, le `dropdb`, le chargement, puis
      **« réapplication des ACL »**.
- [ ] Un répertoire `pre-restore-*` existe dans `/tmp/pra-backups`.

```bash
sudo -u postgres psql -d pra -tAc "SELECT count(*), sum(montant) FROM facture"
```

- [ ] Les **500** lignes sont revenues, et la somme est celle notée en 1.

### 5. Ce qu'aucun dump ne porte

C'est le contrôle qui compte le plus : les ACL ne sont ni dans le dump ni dans
`globals.sql`, et leur absence ne produit **aucun message**.

```bash
sudo -u postgres psql -tAc \
  "SELECT array_to_string(datacl, ' ') FROM pg_database WHERE datname='pra'"
sudo -u postgres psql -d pra -tAc \
  "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tableowner <> 'pra'"
/usr/local/bin/pg verify pra
```

- [ ] Le `datacl` ne contient **pas** d'entrée dont le bénéficiaire est vide —
      c'est ainsi que PostgreSQL écrit `PUBLIC`. Autrement dit : `PUBLIC` ne
      peut pas se connecter.
- [ ] **Zéro** table n'appartient à quelqu'un d'autre que `pra` : le `--role`
      de `pg_restore` a bien été passé.
- [ ] `pg verify pra` n'émet aucun `[WARN ]`.

### 6. Le cas qui sortait en erreur

Le bash rendait **1** sur une restauration réussie quand la base n'existait
pas : sa dernière instruction testait un filet qui n'avait pas eu lieu d'être.

```bash
sudo -u postgres dropdb pra
PG_BACKUP_DEST=/tmp/pra-backups /usr/local/bin/pg restore pra --yes
echo "code de retour : $?"
```

- [ ] **Code de retour 0**, et la base est bien recréée avec ses 500 lignes.
- [ ] Le journal dit « la base pra n'existe pas — création » et **aucun** filet
      `pre-restore-*` n'est créé pour cette exécution : il n'y avait rien à
      sauver.

### 7. Comparer au moteur bash

```bash
# Ici pas de sudo : pgbk tourne en root et lit la variable directement.
PG_BACKUP_DEST=/tmp/pra-backups /usr/local/bin/pgbk restore pra --yes --local
echo "code de retour bash : $?"
sudo -u postgres psql -d pra -tAc "SELECT count(*) FROM facture"
```

- [ ] Le bash restaure les mêmes 500 lignes.
- [ ] Écart attendu et **connu** : si la base existait, le bash rend 0 ; si
      elle n'existait pas, il rend 1 alors que la restauration a réussi. C'est
      le défaut que le portage corrige, pas une régression.

### 8. Démonter — ne rien sauter

```bash
sudo -u postgres dropdb pra
sudo -u postgres psql -c "DROP ROLE pra"
rm -rf /tmp/pra-backups /tmp/pra.json
exit
```

- [ ] La base et le rôle `pra` n'existent plus :
      `pct exec 200 -- sudo -u postgres psql -tAc "SELECT datname FROM pg_database WHERE datname='pra'"`
      ne renvoie rien.
- [ ] **`/tmp/pra-backups` est effacé** : il contenait un `globals.sql`, donc
      les empreintes SCRAM de tous les rôles du cluster.
- [ ] `pg list` sur le nœud montre le même nombre d'instantanés qu'avant
      l'exercice, et le même `← latest`.
- [ ] `pg offsite --dry-run` sort en 0 et n'annonce **rien à transférer**.

### 9. Consigner, et décider

- [ ] Ligne ajoutée au journal ci-dessous, type « bascule ».
- [ ] **Décision notée** : le moteur bash peut être retiré, ou non, et
      pourquoi. Tant que cette case n'est pas cochée, `ct/pgbk.sh` reste posé.

### Ce que cet exercice ne couvre pas

- **La restauration depuis GCS.** C'est l'exercice court qui la prouve ; ici on
  restaure depuis un dépôt local fabriqué pour l'occasion.
- **La restauration d'un service.** On rend une base à son propriétaire avec
  ses ACL ; que Forgejo redémarre là-dessus est un autre plan.
- **Le comportement sous charge.** L'exercice ferme des sessions qu'il a
  ouvertes lui-même ; une base réellement occupée est un autre cas.

## Journal des exercices

| Date | Type | Instantané joué | RTO mesuré | Anomalies | Corrigé dans |
|---|---|---|---|---|---|
| 2026-08-21 | bascule | `20260821-110735` (local, dépôt détourné) | sans objet | **4**, détaillées ci-dessous | `851f90f`, `07f263f`, `ced80d1` |
| *(prochain : exercice court, trimestriel)* | | | | | |

### 2026-08-21 — exercice de bascule

Joué sur le CT 200, locataire `pra` (500 lignes), dépôt détourné vers
`/tmp/pra-backups`. La production n'a pas bougé : 8 instantanés avant et après,
même `latest`, rien de nouveau dans le bucket.

**Résultat : le moteur Python restaure correctement.** 500 lignes rendues après
un `DELETE` de 400, somme identique, ACL réappliquées (`pra=CTc/pra`, `PUBLIC`
sans `CONNECT`), aucune table étrangère, code de retour 0.

**Le défaut qui justifiait la bascule est constaté côte à côte**, sur le même
instantané et la même base : restaurer une base qui n'existe pas rend **0** en
Python et **1** en bash, alors que les deux réussissent et produisent le même
état. Un appelant qui vérifie le code conclurait à un échec.

**Quatre anomalies, toutes corrigées dans le dépôt** — c'est pour les trouver
qu'on joue l'exercice :

| Ce qui n'allait pas | Conséquence | Corrigé dans |
|---|---|---|
| Le filet `pre-restore-*` était créé par root, sans `chown` vers `postgres` | le `pg_dump` du filet aurait été refusé, **au moment précis où l'on sauvegarde avant d'écraser** | `851f90f` |
| La procédure écrivait `VAR=… sudo …` | `sudo` efface l'environnement : la sauvegarde d'exercice serait partie en production puis **dans le bucket**, où rien ne peut l'effacer | `851f90f` |
| `psql -c` ne substitue pas les variables `:"var"` | la **réapplication des ACL n'a jamais fonctionné** depuis la création de `restore.py` | `07f263f` |
| Le contrôle d'isolation passait le `datacl` en minuscules et découpait sur la virgule | `C` (CREATE) confondu avec `c` (CONNECT), et une seule entrée examinée : faux positif d'un côté, **faux négatif** de l'autre | `ced80d1` |

La troisième est la plus grave : sans cet exercice, la bascule se serait faite
avec une réapplication des ACL morte, et on l'aurait découvert un jour de
restauration réelle — en constatant que `PUBLIC` peut se connecter à une base
de locataire.

**Décision :** *(à cocher après le démontage — tant qu'elle n'est pas prise,
`ct/pgbk.sh` reste posé)*

- [ ] Le moteur bash peut être retiré du conteneur (étape 8 de la migration).


## Ce que l'exercice ne couvre pas

À savoir, pour ne pas confondre « exercice réussi » et « on est couvert » :

- **Le RPO reste de 24 h.** Aucun exercice ne rattrape l'absence d'archivage
  WAL ; ce qui a été écrit depuis la dernière sauvegarde est perdu.
- **La perte du dépôt git** n'est pas jouée : `pg deploy` en vient. Le
  dépôt doit être poussé ailleurs que sur le nœud — un Forgejo hébergé sur ce
  même nœud ne compte pas.
- **La perte d'OpenBao** n'est pas jouée non plus. Le
  [scénario 6](PRA.md#6--les-secrets-sont-perdus) explique pourquoi elle est
  moins grave qu'il n'y paraît, mais elle empêche toute nouvelle connexion
  administrative en dehors de la porte `peer`.
- **La restauration des applications** (Forgejo et consorts) n'est pas
  couverte ici : ce PRA rend une base, pas un service. Chaque service porte
  son propre plan.
