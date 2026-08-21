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
> toutes les commandes `pg` suivantes viseraient le CT d'exercice, y compris un
> `pg restore` fait en urgence trois semaines plus tard. Le démontage le
> remet — ne pas sauter cette étape.

Troisième règle, qui ne relève pas d'un drapeau : **l'exercice ne lit que le
bucket, il n'y écrit rien.** La récupération se fait avec le compte personnel
(`gcloud`), jamais avec la clé du nœud.

## Trois machines — où se tape quoi

C'est la question qu'on se pose à 4 h du matin, et la seule que ce document ne
peut pas se permettre de laisser implicite. **Chaque bloc de commandes commence
par une bannière qui nomme sa machine.** Si vous ne savez plus où vous êtes,
`hostname` répond.

| Machine | Comment y aller | Comment en revenir | Ce qui s'y fait |
|---|---|---|---|
| **NŒUD** `pve-eranikus`, `192.168.1.11` | `ssh root@192.168.1.11` | — | tout, sauf les deux lignes ci-dessous |
| **CT 200 / 299** | `pct enter <ctid>`, depuis le nœud | `exit` | ce qui doit atteindre le moteur **Python** du conteneur |
| **POSTE d'administration** | c'est votre machine, pas le nœud | — | la récupération GCS avec le compte **personnel** |

**Par défaut, on est sur le nœud.** Les deux autres ne servent qu'à ce qu'on ne
peut pas y faire :

- le **poste**, parce que la récupération doit prouver qu'elle fonctionne
  **sans aucun secret du nœud** — c'est tout l'objet du scénario 4 ;
- le **conteneur**, parce que le moteur Python n'y est pas encore atteignable
  depuis le nœud. `pg restore` tapé sur le nœud délègue à `/usr/local/bin/pgbk`,
  le moteur **bash**. Tant que la bascule n'est pas faite, valider le Python
  demande d'entrer dans le CT. C'est précisément ce que l'exercice de bascule
  existe pour lever.

**Un nom d'instantané, posé une fois.** Chaque exercice commence par le choisir
et le mettre dans `SNAP` ; tout le reste s'y réfère. Le retaper douze fois est
le meilleur moyen de restaurer le mauvais — et la variable doit être reposée
sur chaque machine, elle ne traverse ni `ssh` ni `pct enter`.

## Exercice court

Prouve que ce qui est parti dans GCS est bien ce qui était sur le disque, et
que ça se recharge. Se joue sur le CT 200 sans y toucher.

### 1. Choisir l'instantané — sur le nœud

```bash
# ══ NŒUD pve-eranikus ═══════════════════════════════════════════
pg list                          # ce qui est sur le disque du CT

rclone --config /root/.config/rclone/rclone.conf \
       lsf gcs:homelab-pgsql-backups-dc93212a/pve-eranikus/postgresql/

# Choisir un nom présent dans LES DEUX listes, et le poser une fois pour toutes.
export SNAP=20260821-023639
echo "$SNAP"
```

- [ ] `$SNAP` figure à la fois dans `pg list` et dans la liste du bucket. S'il
      n'est que d'un côté, la chaîne hors-site a déjà un problème : c'est le
      moment de s'arrêter et de regarder pourquoi.

### 2. Récupérer depuis GCS — sur le poste d'administration

Avec le **compte personnel**, jamais la clé du nœud. C'est ce qui prouve qu'une
récupération reste possible le jour où le nœud n'existe plus.

```bash
# ══ POSTE D'ADMINISTRATION — pas le nœud ════════════════════════
export SNAP=20260821-023639      # le même nom qu'à l'étape 1

mkdir -p ~/pra && cd ~/pra
gcloud auth login
gcloud storage cp -r \
  gs://homelab-pgsql-backups-dc93212a/pve-eranikus/postgresql/"$SNAP" .

ls -l "$SNAP"
cat "$SNAP/MANIFEST"
```

- [ ] Le `MANIFEST` porte la bonne date, la bonne version PostgreSQL, et la
      liste des bases attendue.
- [ ] La récupération n'a demandé **aucun secret du nœud**.

### 3. Rapatrier sur le nœud

Tout le reste se joue sur le nœud : c'est lui qui a `pct`. Sans ce transfert,
l'étape 5 n'aurait rien à pousser dans le conteneur — c'est le trou que suivre
ce document à 4 h du matin faisait découvrir trop tard.

```bash
# ══ POSTE D'ADMINISTRATION — pas le nœud ════════════════════════
# toujours dans ~/pra
scp -r "$SNAP" root@192.168.1.11:/tmp/
```

- [ ] Sur le nœud, `ls /tmp/$SNAP` montre les fichiers de l'instantané.

### 4. Comparer au local — la copie est-elle fidèle ?

Les deux empreintes se calculent **sur le nœud**, donc sans avoir à comparer
des chiffres lus sur deux écrans différents.

```bash
# ══ NŒUD pve-eranikus ═══════════════════════════════════════════
# La vue HÔTE du dataset est écrite dans le drop-in par « pg deploy » :
# la lire plutôt que la recopier de mémoire.
SRC=$(sed -n 's/^Environment=PGBK_OFFSITE_SRC=//p' \
      /etc/systemd/system/pgbk-offsite.service.d/10-noeud.conf)
echo "original : $SRC/$SNAP"
echo "copie    : /tmp/$SNAP"

# « cd » puis « md5sum * » : les empreintes portent le nom du fichier SEUL,
# donc les deux listes se comparent directement.
( cd "$SRC/$SNAP" && md5sum * ) | sort > /tmp/pra-original.txt
( cd "/tmp/$SNAP" && md5sum * ) | sort > /tmp/pra-copie.txt

diff /tmp/pra-original.txt /tmp/pra-copie.txt && echo "IDENTIQUES"
```

- [ ] `diff` ne dit rien et affiche **IDENTIQUES**, fichier par fichier. C'est
      le contrôle qui prouve la chaîne hors-site ; le reste de l'exercice teste
      la restauration, pas la copie.

### 5. Restaurer dans une base jetable — depuis le nœud

Rien n'oblige à entrer dans le conteneur : `pct exec` suffit, et on reste sur
une seule machine.

```bash
# ══ NŒUD pve-eranikus ═══════════════════════════════════════════
pct push 200 "/tmp/$SNAP/forgejo.dump" /tmp/pra.dump

pct exec 200 -- sudo -u postgres createdb forgejo_pra -O forgejo -T template0 \
     --encoding UTF8 --lc-collate C --lc-ctype C
pct exec 200 -- sudo -u postgres pg_restore -d forgejo_pra --no-owner \
     --role=forgejo /tmp/pra.dump
```

- [ ] `pg_restore` se termine sans erreur.

```bash
# ══ NŒUD pve-eranikus ═══════════════════════════════════════════
pct exec 200 -- sudo -u postgres psql -d forgejo_pra -c "\dt"
pct exec 200 -- sudo -u postgres psql -d forgejo_pra -tAc \
  "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
```

- [ ] Le nombre de tables correspond à celui de la base de production.
- [ ] Les tables appartiennent à `forgejo`, pas à `postgres` — sinon le
      `--role` a été oublié quelque part.
- [ ] Deux ou trois tables métier contiennent des lignes cohérentes avec la
      date de l'instantané.

### 6. Démonter

```bash
# ══ NŒUD pve-eranikus ═══════════════════════════════════════════
pct exec 200 -- sudo -u postgres dropdb forgejo_pra
pct exec 200 -- rm -f /tmp/pra.dump
rm -rf "/tmp/$SNAP" /tmp/pra-original.txt /tmp/pra-copie.txt
```

```bash
# ══ POSTE D'ADMINISTRATION — pas le nœud ════════════════════════
rm -rf ~/pra/"$SNAP"
```

- [ ] La base jetable est supprimée, le dump temporaire aussi.
- [ ] **Les deux copies sont effacées**, celle du nœud et celle du poste :
      `globals.sql` porte les empreintes SCRAM de tous les rôles du cluster.

## Exercice complet

Rejoue le [scénario 4](PRA.md#4--le-nœud-est-perdu) — nœud perdu — sur un
conteneur jetable. Le seul écart avec la réalité est qu'on ne rachète pas de
matériel : le CT 299 tient lieu de machine de remplacement.

### 0. État de départ

```bash
# ══ NŒUD pve-eranikus ═══════════════════════════════════════════
cd /root/homelab_proxmox        # « pg deploy » se lit depuis le dépôt

date                            # heure de début, à noter
pve-eranikus/pgsql/pg deploy --status     # la production est saine ?
cat /etc/default/pgbk           # doit dire PG_CTID=200

# Le nom de l'instantané à rejouer, posé une fois.
export SNAP=20260821-023639
```

- [ ] La production est conforme **avant** de commencer. On ne joue pas un
      exercice par-dessus une anomalie.
- [ ] `PG_CTID=200` est noté quelque part pour le démontage.
- [ ] `$SNAP` est choisi, et il est présent dans le bucket.

**Début : ____h____**

### 1. Créer le conteneur d'exercice

Script communautaire ([runbook § 1](RUNBOOK.md#1-création-du-conteneur)),
CTID **299**, mêmes réponses qu'en production — **nesting = oui** — avec une IP
libre et un disque plus petit.

- [ ] CT 299 créé et démarré.

**Durée mesurée : ______**

### 2. Reposer le service

```bash
# ══ NŒUD pve-eranikus ═══════════════════════════════════════════
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
# ══ POSTE D'ADMINISTRATION — pas le nœud ════════════════════════
export SNAP=20260821-023639      # le même nom qu'à l'étape 0

mkdir -p ~/pra && cd ~/pra
gcloud auth login
gcloud storage cp -r \
  gs://homelab-pgsql-backups-dc93212a/pve-eranikus/postgresql/"$SNAP" .
cat "$SNAP/MANIFEST"

# rapatriement vers le NŒUD : 192.168.1.11, c'est lui qui a « pct »
scp -r "$SNAP" root@192.168.1.11:/tmp/
```

- [ ] La récupération n'a demandé **aucun secret du nœud**. C'est le point
      central du scénario 4 : si un accès a manqué ici, le PRA est faux.

**Durée mesurée : ______**

### 4. Charger dans le CT 299

```bash
# ══ NŒUD pve-eranikus ═══════════════════════════════════════════
# /tmp/$SNAP vient de l'étape 3 : le scp l'a déposé ICI, sur le nœud.
DEST=/var/backups/postgresql/$SNAP

pct exec 299 -- mkdir -p "$DEST"
for f in "/tmp/$SNAP"/*; do
  pct push 299 "$f" "$DEST/$(basename "$f")"
done
pct exec 299 -- chown -R postgres:postgres "$DEST"
pct exec 299 -- chmod 700 "$DEST"

# les rôles D'ABORD : « restore » refuse une base dont le rôle propriétaire
# n'existe pas, et c'est le rappel que globals.sql passe en premier
pct exec 299 -- sudo -u postgres psql -f "$DEST/globals.sql"

pg --ctid 299 show    "$SNAP"
pg --ctid 299 restore forgejo "$SNAP"
pg --ctid 299 verify  forgejo
```

> Ces trois dernières commandes se tapent **sur le nœud** et sont acheminées
> vers le CT 299. Elles atteignent aujourd'hui le moteur **bash** — c'est
> volontaire ici : cet exercice éprouve le scénario « nœud perdu », pas le
> portage Python. C'est l'exercice de bascule qui éprouve le second.

- [ ] `globals.sql` recrée les rôles sans erreur.
- [ ] `pg restore` va au bout.
- [ ] `pg verify` montre une ACL non vide et « propriétaire des tables : OK ».

**Durée mesurée : ______**

### 5. Contrôles fonctionnels

Le vrai test n'est pas que `pg_restore` finisse, c'est qu'un client puisse
travailler :

```bash
# ══ NŒUD pve-eranikus ═══════════════════════════════════════════
# nombre de tables et de lignes, à comparer à la production
pct exec 299 -- sudo -u postgres psql -d forgejo -c "\dt"
pct exec 299 -- sudo -u postgres psql -c "\l forgejo"   # ACL : doit afficher =T/forgejo

pct enter 299          # ↓ la suite se tape DANS le conteneur
```

```bash
# ══ DANS LE CT 299 — après « pct enter 299 » ════════════════════
# Connexion en tant que locataire, comme le ferait le service. Ici « pct enter »
# et non « pct exec » : ce dernier n'alloue pas de TTY, psql ne pourrait pas
# demander le mot de passe.
psql "postgresql://forgejo@127.0.0.1/forgejo?sslmode=require" -c "\dt"

exit                   # ↑ retour sur le nœud
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
# ══ NŒUD pve-eranikus ═══════════════════════════════════════════
cd /root/homelab_proxmox

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

rm -rf "/tmp/$SNAP"               # empreintes SCRAM
```

```bash
# ══ POSTE D'ADMINISTRATION — pas le nœud ════════════════════════
rm -rf ~/pra/"$SNAP"              # l'autre copie, celle du poste
```

- [ ] CT 299 détruit, son volume `mp2` avec.
- [ ] `/etc/default/pgbk` dit de nouveau `PG_CTID=200`.
- [ ] Le drop-in `pgbk-offsite` pointe toujours le volume du CT **200** :
      `grep SRC /etc/systemd/system/pgbk-offsite.service.d/10-noeud.conf`.
- [ ] Les deux timers sont armés, sur l'hôte et dans le CT.
- [ ] Les **deux** copies contenant `globals.sql` sont effacées : `/tmp/$SNAP`
      sur le nœud, `~/pra/$SNAP` sur le poste.

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
>
> **La variable traverse la frontière depuis le 21 août 2026.** `pct exec`
> n'hérite d'aucun environnement : avant, `PG_BACKUP_DEST=… pg restore` tapé
> **depuis le nœud** visait le dépôt de production sans que rien ne le dise —
> la commande réussissait, elle faisait autre chose. `pg` la transmet
> désormais explicitement, et elle seule. Les étapes ci-dessous se jouent
> depuis `pct enter 200`, où elle a toujours été locale au conteneur ; le
> geste depuis le nœud fait maintenant ce qu'il annonce.

Les deux garde-fous des autres exercices ne s'appliquent pas ici : on ne crée
pas de conteneur, et on ne touche pas à `/etc/default/pgbk`.

### Où l'on est, et pourquoi on n'en sort pas

**Tout, à partir de la fin de l'étape 1, se tape DANS le CT 200.** Ce n'est pas une
préférence : `pg restore` tapé sur le nœud délègue à `/usr/local/bin/pgbk`, le
moteur **bash**. On éprouverait donc exactement ce qu'on cherche à remplacer,
et l'exercice réussirait sans rien prouver.

Le moteur Python est en `/usr/local/bin/pg` **dans le conteneur** ; c'est ce
chemin-là, absolu, qu'emploient les commandes ci-dessous. Cette gêne est
précisément ce que l'exercice existe pour lever : une fois la décision prise,
le nœud délèguera au Python et l'exercice suivant tiendra sur une machine.

Un seul aller-retour : `pct enter 200` à la fin de l'étape 1, `exit` à
l'étape 8.

### 1. Un locataire jetable

```bash
# ══ NŒUD pve-eranikus ═══════════════════════════════════════════
cd /root/homelab_proxmox
pve-eranikus/pgsql/pg deploy --tenant pra
```

Le mot de passe s'affiche une fois. **Il n'a pas besoin d'être conservé** : ce
locataire sera supprimé au démontage, et l'exercice se connecte en `peer`.

```bash
# ══ NŒUD pve-eranikus ═══════════════════════════════════════════
pct enter 200          # ↓ on y reste jusqu'à l'étape 8
```

```bash
# ══ DANS LE CT 200 — après « pct enter 200 » ════════════════════
sudo -u postgres psql -d pra -c "
  CREATE TABLE facture (id serial PRIMARY KEY, montant numeric, pose date);
  INSERT INTO facture (montant, pose)
  SELECT g * 10.5, current_date - g FROM generate_series(1, 500) g;
  ALTER TABLE facture OWNER TO pra;"
sudo -u postgres psql -d pra -tAc "SELECT count(*), sum(montant) FROM facture"
```

- [ ] La base `pra` contient **500** lignes. Noter la somme : _______________
- [ ] `hostname` répond le nom du CONTENEUR, pas `pve-eranikus`. Si ce n'est
      pas le cas, le `pct enter` n'a pas pris et tout ce qui suit toucherait le
      nœud.

### 2. Une sauvegarde, hors du dépôt de production

```bash
# ══ DANS LE CT 200 — après « pct enter 200 » ════════════════════
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
# ══ DANS LE CT 200 — après « pct enter 200 » ════════════════════
sudo -u postgres psql -d pra -c "DELETE FROM facture WHERE id > 100"
sudo -u postgres psql -d pra -tAc "SELECT count(*) FROM facture"
```

- [ ] Il ne reste que **100** lignes. C'est le `DELETE` parti trop loin du
      [scénario 1](PRA.md#1--une-base-perdue-ou-corrompue).

### 4. Restaurer avec le moteur Python

```bash
# ══ DANS LE CT 200 — après « pct enter 200 » ════════════════════
# /usr/local/bin/pg : le moteur PYTHON, dans le conteneur. Chemin absolu et
# non « pg » : le PATH d'un shell de conteneur n'est pas garanti.
PG_BACKUP_DEST=/tmp/pra-backups /usr/local/bin/pg restore pra --yes
echo "code de retour : $?"
```

- [ ] **Code de retour 0.**
- [ ] Le journal montre, dans cet ordre : le propriétaire capturé, le filet
      `pre-restore-*`, les sessions fermées, le `dropdb`, le chargement, puis
      **« réapplication des ACL »**.
- [ ] Un répertoire `pre-restore-*` existe dans `/tmp/pra-backups`.

```bash
# ══ DANS LE CT 200 — après « pct enter 200 » ════════════════════
sudo -u postgres psql -d pra -tAc "SELECT count(*), sum(montant) FROM facture"
```

- [ ] Les **500** lignes sont revenues, et la somme est celle notée en 1.

### 5. Ce qu'aucun dump ne porte

C'est le contrôle qui compte le plus : les ACL ne sont ni dans le dump ni dans
`globals.sql`, et leur absence ne produit **aucun message**.

```bash
# ══ DANS LE CT 200 — après « pct enter 200 » ════════════════════
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
# ══ DANS LE CT 200 — après « pct enter 200 » ════════════════════
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
# ══ DANS LE CT 200 — après « pct enter 200 » ════════════════════
# /usr/local/bin/pgbk : le moteur BASH, celui qu'on remplace. Même conteneur,
# même dépôt détourné, même base — seule l'implémentation change.
# Pas de sudo ici : pgbk tourne en root et lit la variable directement.
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
# ══ DANS LE CT 200 — après « pct enter 200 » ════════════════════
sudo -u postgres dropdb pra
sudo -u postgres psql -c "DROP ROLE pra"
rm -rf /tmp/pra-backups /tmp/pra.json

exit                   # ↑ retour sur le nœud
```

```bash
# ══ NŒUD pve-eranikus ═══════════════════════════════════════════
pg list                # même nombre d'instantanés qu'avant, même ← latest
pg offsite --dry-run   # doit sortir en 0 et n'annoncer rien à transférer
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
