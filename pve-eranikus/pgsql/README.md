# CT PostgreSQL mutualisé — `pve-eranikus`

Cluster PostgreSQL unique servant les services LXC du nœud. Un couple
base + rôle par locataire, isolés les uns des autres.

Ce fichier ne porte que **ce qu'on tape**. Le reste est dans `doc/` :

| | |
|---|---|
| [doc/RUNBOOK.md](doc/RUNBOOK.md) | le détail — création du conteneur, conception, pièges rencontrés en production |
| [doc/PRA.md](doc/PRA.md) | **les mauvais jours** — une procédure de reprise par scénario, du `DELETE` malheureux au nœud parti en fumée |
| [doc/PRA-exercice.md](doc/PRA-exercice.md) | comment jouer le PRA pour de faux, et mesurer ce qu'il coûte vraiment |

| | |
|---|---|
| CTID | 200, hostname `postgresql` |
| IP | 192.168.1.56/24, passerelle 192.168.1.254 |
| Nœud | `pve-eranikus` (192.168.1.11), Debian 13 |
| PostgreSQL | 18.6, dépôt PGDG, cluster `18/main` |
| Base système | `local-lvm` (SSD 512 Go) |
| Sauvegardes | `mp2` → `/var/backups/postgresql`, 50 Go sur `data` (NVMe), 14 j |
| Hors-site | `gs://homelab-pgsql-backups-dc93212a/pve-eranikus/postgresql/`, 3h30 |
| Dépôt monté | `/root/homelab_proxmox/pve-eranikus/pgsql/ct` → `/etc/pgsql-git` (ro) |

## Déployer, mettre à jour

**Une seule commande, depuis le nœud, sans entrer dans le CT.** Première pose
et mise à jour, c'est la même : chaque étape est conditionnelle et ne touche à
rien si l'état est déjà conforme.

```bash
cd /root/homelab_proxmox && git pull
pve-eranikus/pgsql/pg deploy
```

L'enchaîner à chaque `git pull` est le geste normal : les scripts et les unités
sont des **copies**, pas des symlinks, et ne suivent pas le `git pull` seuls.

> **La bascule est faite.** `pg-deploy.sh` a été retiré le 21 août 2026, après
> comparaison des deux `--status` sur le nœud : les 35 lignes du bilan bash
> avaient chacune leur contrepartie exacte. `pg deploy` en rapporte sept de
> plus — dont la protection du conteneur, que le bash levait et rétablissait
> sans jamais le dire.

Elle installe les paquets manquants (`rclone`, `sudo`), pose les points de
montage — dont le volume des sauvegardes —, la configuration, les scripts, les
unités systemd et `rclone.conf`, puis déclenche la première sauvegarde et la
première copie hors-site. Détail :
[runbook § 2](doc/RUNBOOK.md#2-déploiement-depuis-lhôte--pg-deploy).

```bash
pg deploy --status        # état de chaque élément, ne change rien
pg deploy --dry-run       # annonce ce qui serait fait, effets compris
pg deploy --ctid 201      # cible un autre conteneur, et le consigne
pg deploy --restart       # force un restart au lieu d'un reload
pg deploy --no-container  # ne touche pas au CT (le hors-site ne s'armera pas)
pg deploy --no-offsite    # saute la copie hors-site
pg deploy --no-install    # n'installe aucun paquet (nœud sans réseau)
pg deploy --no-first-run  # ne déclenche ni sauvegarde ni copie initiale
```

Sur un CT déjà conforme, `--dry-run` doit annoncer **zéro modification**.

**`--status` et `--dry-run` ne sont pas la même chose.** Le premier rend des
verdicts — OK, POSE, KO, avec leur motif. Le second annonce en plus **chaque
modification qu'il ferait**, redémarrage du conteneur compris. Un drapeau
`--no-*` ne désactive jamais un contrôle, seulement une pose : le bilan reste
complet quels que soient les drapeaux.

`pg deploy` se joue **depuis le dépôt** — c'est de là qu'il lit ce qu'il pose,
et la copie installée dans `/usr/local/sbin` n'aurait rien à en dire. Les
exemples ci-dessous omettent le préfixe
`/root/homelab_proxmox/pve-eranikus/pgsql/`. Les autres commandes `pg`, elles,
sont bien dans le `PATH` du nœud.

**Deux choses qu'il ne fait pas**, délibérément : créer le conteneur (script
communautaire, [§ 1](doc/RUNBOOK.md#1-création-du-conteneur)) et déposer la clé du
compte de service GCP, qui est un secret
([§ 10](doc/RUNBOOK.md#10-copie-hors-site-vers-gcs--pgbk-offsite)).

## Gestes courants

Tout se tape **sur le nœud**, pas dans le CT.

```bash
pg status                          # l'état du montage — À COMMENCER PAR LÀ
pg list                            # instantanés : âge, taille, bases
pg backup                          # sauvegarde immédiate
pg show 20260820-093240            # MANIFEST + fichiers
pg restore forgejo                 # depuis le dernier instantané
pg restore forgejo 20260819        # depuis le plus récent de ce jour
pg verify forgejo                  # contrôle ACL et propriétaires
pg delete 20260819-233627          # supprime un instantané (jamais le dernier)
pg delete 20260819 --plan          # dit lequel serait visé, n'efface rien
pg --ctid 299 list                 # vise un autre conteneur, ponctuellement
```

**`pg status` est la commande à taper quand on se demande si tout va bien.**
Elle regarde ensemble les trois maillons qui peuvent se rompre en silence : la
sauvegarde locale, le timer qui la déclenche dans le CT, et la copie hors-site
armée sur le nœud. `pg deploy --status` répond à une autre question — celle de
savoir si les fichiers sont en place.

Elle sort en 1 dès qu'une alarme est levée, et **un maillon non constaté est
une alarme**, pas un silence : un bucket qui n'a pas répondu ne vaut pas un
bucket cohérent. Les seuils viennent des unités elles-mêmes — la sauvegarde
tourne à 2h30, donc au-delà de 26 heures une exécution a été manquée.

`pg` achemine vers le moteur du conteneur, qui fait le travail. Les questions
de confirmation sont posées **ici**, sur le nœud : `pct exec` n'alloue pas de
TTY, une question posée depuis le conteneur ne verrait jamais la réponse. Pour
`delete`, la question porte sur l'instantané **réellement visé** et non sur ce
qui a été tapé — `20260819` désigne la plus récente de ce jour-là.

`pgbk` reste installé le temps de constater la parité — **des deux côtés** : le
moteur Python est posé dans le CT à côté du bash, et c'est encore le bash qui
tourne tant qu'une répétition de restauration n'a pas été jouée.

Créer un compte ou un locataire — le mot de passe généré n'est affiché
**qu'une fois**, et rien ne bouge si le rôle existe déjà :

```bash
pg deploy --admin  jbwittner    # compte d'administration
pg deploy --tenant forgejo      # base + rôle d'un service
```

Reste ensuite **un** geste manuel : ajouter la ligne du locataire dans
`pg_hba.conf`, avant le `reject`, puis rejouer `pg deploy`.

La copie hors-site se joue aussi à la main, sur le nœud :

```bash
pg offsite --dry-run   # ce qui partirait, et les divergences déjà détectables
pg offsite             # ce que fait le timer de 3h30
```

Journaux :

```bash
pct exec 200 -- journalctl -u pg-backup -n 50 --no-pager   # sauvegarde locale
journalctl -u pgbk-offsite -n 50 --no-pager                # copie hors-site
journalctl -u pgbk-offsite -p warning                      # anomalies seules
```

## Où va chaque fichier

Ce répertoire porte des fichiers pour **deux machines**, et le découpage le dit.
**`ct/` est la charge utile du montage** — lui seul est monté en
`/etc/pgsql-git`, en lecture seule. **`host/`** est ce qui s'installe sur le
nœud, et que le conteneur ne voit pas : ni le nom du bucket, ni le chemin de la
clé GCS. Le lanceur `pg`, ce fichier et `doc/` restent à la racine du service.

| Fichier | Tourne sur | Installé en |
|---|---|---|
| `pg`, `pgtool/` + `lib/` (racine du dépôt) | **hôte** | `/usr/local/sbin/pg`, arbre d'import en `/usr/local/lib/pgtool` |
| `ct/pgbk.sh` | **hôte** et **CT** | `/usr/local/sbin/pgbk` (hôte), `/usr/local/bin/pgbk` (CT) |
| `pgtool/` + `lib/core/` poussés par `pct push` | **CT 200** | `/usr/local/lib/pgtool/`, lanceur en `/usr/local/bin/pg` |
| `host/pgbk-offsite.service` / `.timer` | **hôte** | `/etc/systemd/system/` de l'hôte |
| `ct/pg-backup.sh` | **CT 200** | `/usr/local/bin/pg-backup.sh` |
| `ct/pg-backup.service` / `.timer` | **CT 200** | `/etc/systemd/system/` du CT |
| `ct/10-homelab.conf`, `ct/pg_hba.conf` | **CT 200** | symlinks depuis `/etc/pgsql-git` |
| `ct/tenant.sql` | **CT 200** | joué par `pg deploy --tenant` |

Les chemins **`/etc/pgsql-git/<fichier>`** sont stables : c'est le contrat du
montage. Le conteneur ne voit plus `doc/` — le runbook se lit depuis le nœud.
Pourquoi ce découpage : [runbook § 3](doc/RUNBOOK.md#3-montage-du-dépôt).

Le dataset de sauvegarde porte **deux noms selon le point de vue**, et c'est la
confusion la plus facile à faire ici :

| Vu du CT | Vu de l'hôte |
|---|---|
| `/var/backups/postgresql` | `/data/subvol-200-disk-0` |

`pg-backup.sh` écrit dans le premier, `pg offsite` lit le second. Ce sont les
mêmes octets.

## En cas de pépin

**Quelque chose est perdu ?** Aller directement au
[PRA](doc/PRA.md#trouver-son-scénario) : il commence par une table de
diagnostic et donne une procédure complète par scénario.

Avant de chercher : **`pg status`** dit lequel des trois maillons est rompu, et
sort en 1 s'il y en a un.

| Symptôme | Où regarder |
|---|---|
| Une base est corrompue, un `DELETE` est parti trop loin | [PRA § 1](doc/PRA.md#1--une-base-perdue-ou-corrompue) |
| PostgreSQL ne démarre plus | [PRA § 2](doc/PRA.md#2--le-cluster-ne-démarre-plus) |
| Le CT 200 ou le nœud a disparu | [PRA § 3](doc/PRA.md#3--le-conteneur-est-détruit) et [§ 4](doc/PRA.md#4--le-nœud-est-perdu) |
| Restaurer une base, cas ordinaire | [runbook § 8](doc/RUNBOOK.md#8-pg--interface-de-gestion), ou [§ 9](doc/RUNBOOK.md#9-restauration-manuelle) à la main |
| Récupérer une sauvegarde depuis GCS | [runbook § 10](doc/RUNBOOK.md#restauration-depuis-gcs) |
| `pgbk-offsite` sort en code 3 | objet distant divergent, [§ 10](doc/RUNBOOK.md#objet-distant-divergent--le-cas-à-traiter-à-la-main) — intervention humaine |
| `pgbk-offsite.timer` reste inactif | clé GCP, `rclone` ou `mp2` : le bilan de `pg deploy` nomme le prérequis manquant ([§ 10](doc/RUNBOOK.md#installation)) |
| Erreur 400 « legacy ACL » | accès uniforme du bucket, [§ 10](doc/RUNBOOK.md#le-piège-de-laccès-uniforme-ubla) |
| Base injoignable, service `active` | `listen_addresses` en LXC, [§ 4](doc/RUNBOOK.md#4-pose-de-la-configuration) |
| Après restauration, isolation disparue | les ACL ne sont pas dans le dump, [§ 9](doc/RUNBOOK.md#les-acl-ne-sont-pas-dans-le-dump) |
| CT en `243/CREDENTIALS` | nesting, [§ 1](doc/RUNBOOK.md#le-piège-du-nesting) |

## Reste à faire

- [ ] **Constater la parité de `pg <commande>`** avec `pgbk` dans le CT, puis
      retirer `ct/pgbk.sh` — il reste installé exprès, c'est le filet du
      conteneur tant que la restauration Python n'a pas été éprouvée.
- [ ] **Retirer** la ligne du locataire `forgejo` de `ct/pg_hba.conf`, et sa
      base si elle a été créée. Elle n'a plus d'objet : Forgejo a désormais
      **son propre cluster, co-localisé dans le CT 400** — sur ce nœud-ci, mais
      hors de ce conteneur-ci. Le motif n'est pas la disponibilité, c'est que
      ce cluster-ci se met à jour par script communautaire là où le CT 400 a sa
      version gelée, et que ses fenêtres de maintenance sont celles de son
      locataire le plus bruyant — voir
      [../forgejo/doc/RUNBOOK.md § 3](../forgejo/doc/RUNBOOK.md#3-postgresql-co-localisé).
      Tant qu'elle est là, elle décrit un accès qui ne servira jamais.
- [ ] Copier `postgresql.vars` dans ce dépôt après vérification des secrets.
- [ ] **Jouer l'exercice de bascule**
      ([doc/PRA-exercice.md](doc/PRA-exercice.md#exercice-de-bascule--valider-le-moteur-python)) —
      c'est lui, et lui seul, qui autorise à retirer le moteur bash du CT.
- [ ] **Jouer le premier exercice de PRA** ([doc/PRA-exercice.md](doc/PRA-exercice.md)) —
      tant qu'il ne l'a pas été, le RTO est inconnu et le plan n'est pas prouvé.
- [x] Sauvegarde locale, `pgbk`, copie hors-site GCS, et pose complète par
      `pg deploy` — voir le runbook.
- [x] `pg status` : les trois maillons du montage regardés ensemble.
- [x] Retrait de `pg-deploy.sh` et du hors-site bash, parité constatée
      le 21 août 2026.
