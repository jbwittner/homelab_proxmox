# CT Forgejo — `pve-eranikus`

Instance Forgejo **à version épinglée**, source de vérité d'ArgoCD. Elle doit
rester disponible et réconciliable **quand le cluster Kubernetes est
indisponible** : c'est la contrainte qui prime sur toutes les autres, et c'est
elle qui explique chacune des décisions surprenantes de ce répertoire.

Ce fichier ne porte que **ce qu'on tape**. Le reste est dans `doc/` :

| | |
|---|---|
| [doc/RUNBOOK.md](doc/RUNBOOK.md) | le détail — création du conteneur, conception, pièges rencontrés |
| [doc/PRA.md](doc/PRA.md) | **les mauvais jours** — une procédure de reprise par scénario |
| [doc/PRA-exercice.md](doc/PRA-exercice.md) | comment jouer le PRA pour de faux, et mesurer ce qu'il coûte |

| | |
|---|---|
| CTID | 400, hostname `forgejo` — tier 400–499, [installation manuelle épinglée](../../README.md#le-tier-400499--installations-manuelles) |
| IP | 192.168.1.57/24, passerelle 192.168.1.254 |
| Nœud | `pve-eranikus` (192.168.1.11), Debian 13 |
| Forgejo | branche **15.0 LTS**, fin de support **15 juillet 2027** — version exacte dans [`ct/VERSION`](ct/VERSION) |
| PostgreSQL | **co-localisé dans le CT**, socket Unix + peer, aucune écoute TCP |
| Ingress | Traefik (CT 201) → `forgejo.lan.wittner.tech`, SSH en 2222 |
| Base système | 32 Go minimum |
| Sauvegardes | `mp2` → `/var/backups/forgejo`, 20 Go, 14 j — **base seulement** |
| Dépôts | par `vzdump` du CT — **pas** dans la sauvegarde logique |
| Hors-site | `gs://homelab-pgsql-backups-dc93212a/pve-eranikus/forgejo/`, 03:50 |
| Dépôt monté | `/root/homelab_proxmox/pve-eranikus/forgejo/ct` → `/etc/forgejo-git` (ro) |

> **Ce conteneur ne se met jamais à jour tout seul.** Ni par timer, ni par
> script communautaire. Passer en 16 ou 17 est une décision qui se prend en
> lisant les notes de publication et qui se commite —
> [runbook § 4](doc/RUNBOOK.md#4-la-version-épinglée).

## Déployer, mettre à jour

**Une seule commande, depuis le nœud, sans entrer dans le CT.** Première pose
et mise à jour, c'est la même : chaque étape est conditionnelle et ne touche à
rien si l'état est déjà conforme.

```bash
cd /root/homelab_proxmox && git pull
pve-eranikus/forgejo/fj deploy
```

L'enchaîner à chaque `git pull` est le geste normal : les scripts, les unités
et `app.ini` sont des **copies**, pas des symlinks, et ne suivent pas le
`git pull` seuls.

Elle installe les paquets manquants, pose les points de montage — dont le
volume des sauvegardes —, crée l'utilisateur `git` et l'arborescence, pose la
configuration de PostgreSQL, crée la base et son rôle, **télécharge et vérifie
le binaire épinglé**, dépose les secrets, arme les unités, déclenche la
première sauvegarde et la première copie hors-site. Détail :
[runbook § 2](doc/RUNBOOK.md#2-déploiement-depuis-lhôte--fj-deploy).

```bash
fj deploy --status        # état de chaque élément, ne change rien
fj deploy --dry-run       # annonce ce qui serait fait, effets compris
fj deploy --ctid 401      # cible un autre conteneur, et le consigne
fj deploy --secrets       # autorise la génération des secrets manquants
fj deploy --admin jbwittner   # crée un compte d'administration
fj deploy --restart       # force un restart de PostgreSQL au lieu d'un reload
fj deploy --no-container  # ne touche pas au CT
fj deploy --no-offsite    # saute la copie hors-site
fj deploy --no-install    # n'installe ni paquet ni binaire (nœud sans réseau)
fj deploy --no-first-run  # ne déclenche pas la première sauvegarde
```

Sur un CT déjà conforme, `--dry-run` doit annoncer **zéro modification**.

**`--status` et `--dry-run` ne sont pas la même chose.** Le premier rend des
verdicts — OK, POSE, KO, avec leur motif. Le second annonce en plus **chaque
modification qu'il ferait**, redémarrage du conteneur compris. Un drapeau
`--no-*` ne désactive jamais un contrôle, seulement une pose : le bilan reste
complet quels que soient les drapeaux.

`fj deploy` se joue **depuis le dépôt** — c'est de là qu'il lit ce qu'il pose.
Les exemples ci-dessus omettent le préfixe
`/root/homelab_proxmox/pve-eranikus/forgejo/`. Les autres commandes `fj`, elles,
sont bien dans le `PATH` du nœud.

**Trois choses qu'il ne fait pas**, délibérément : créer le conteneur
([§ 1](doc/RUNBOOK.md#1-création-du-conteneur)), déposer la clé du compte de
service GCP ([§ 10](doc/RUNBOOK.md#10-copie-hors-site-vers-gcs)), et déposer la
clé de publication Forgejo — un ancrage de confiance s'obtient hors du canal
qu'il sert à valider ([§ 4](doc/RUNBOOK.md#la-clé-de-publication)).

## La version épinglée

```bash
fj version              # ce qui est épinglé, et jusqu'à quand c'est supporté
fj version --resolve    # interroge Codeberg, retient la dernière 15.0.x stable,
                        # réécrit ct/VERSION — N'INSTALLE RIEN
```

**Résoudre et poser sont deux commandes.** C'est toute la différence avec le
script communautaire, où « mettre à jour » veut dire « aller chercher la
dernière et la poser » en un geste. Ici `fj deploy` n'interroge personne : il
pose exactement ce que `ct/VERSION` dit.

Après un `--resolve`, **commiter le changement** : l'épinglage n'a de valeur
que tracé. Puis `fj deploy`.

### Depuis un poste de développement

`fj version` ne touche ni à `pct` ni au conteneur : il lit le dépôt et
interroge Codeberg. Il se joue donc très bien depuis le Mac, dans le dépôt,
au moment où l'on s'apprête à commiter l'épinglage.

Une réserve, et elle surprend : le lanceur porte `#!/usr/bin/python3` — un
chemin **absolu**, parce que le PATH de systemd et de `pct exec` est minimal.
Sur macOS, `/usr/bin/python3` est celui des Command Line Tools, souvent bien
plus ancien que le `python3` du PATH. D'où un refus qui n'a rien à voir avec
la version qu'on croit avoir :

```
python3 3.11 minimum requis, 3.9.6 trouvé
(/Library/Developer/CommandLineTools/usr/bin/python3).
```

Passer l'interpréteur explicitement suffit — le lanceur retrouve seul le reste :

```bash
python3 ./pve-eranikus/forgejo/fj version --resolve
```

Le refus dit lui-même quoi taper depuis le 21 août 2026 ; il envoyait
auparavant « installer python3 dans le conteneur », c'est-à-dire corriger la
bonne chose au mauvais endroit.

## Gestes courants

Tout se tape **sur le nœud**, pas dans le CT.

```bash
fj status                          # les maillons du montage — À COMMENCER PAR LÀ
fj list                            # instantanés : âge, taille, version
fj backup                          # sauvegarde immédiate
fj offsite --dry-run               # ce qui partirait hors-site
fj offsite                         # ce que fait le timer de 03:50
```

**`fj status` est la commande à taper quand on se demande si tout va bien.**
Elle regarde ensemble les quatre maillons qui peuvent se rompre en silence :
le service, la sauvegarde locale, le timer qui la déclenche dans le CT, et la
copie hors-site armée sur le nœud. `fj deploy --status` répond à une autre
question — celle de savoir si les fichiers sont en place.

Elle sort en 1 dès qu'une alarme est levée, et **un maillon non constaté est
une alarme**, pas un silence.

Journaux :

```bash
pct exec 400 -- journalctl -u forgejo -n 50 --no-pager      # le service
pct exec 400 -- journalctl -u fj-backup -n 50 --no-pager    # sauvegarde locale
journalctl -u fjbk-offsite -n 50 --no-pager                 # copie hors-site
journalctl -u fjbk-offsite -p warning                       # anomalies seules
```

## Où va chaque fichier

Ce répertoire porte des fichiers pour **deux machines**, et le découpage le dit.
**`ct/` est la charge utile du montage** — lui seul est monté en
`/etc/forgejo-git`, en lecture seule. **`host/`** est ce qui s'installe sur le
nœud, et que le conteneur ne voit pas : ni le nom du bucket, ni le chemin de la
clé GCS.

| Fichier | Tourne sur | Installé en |
|---|---|---|
| `fj`, `fjtool/` + `lib/` (racine du dépôt) | **hôte** | `/usr/local/sbin/fj`, arbre d'import en `/usr/local/lib/fjtool` |
| `fjtool/` + `lib/core/` poussés par `pct push` | **CT 400** | `/usr/local/lib/fjtool/`, lanceur en `/usr/local/bin/fj` |
| `ct/app.ini` | **CT 400** | **copie** en `/etc/forgejo/app.ini` (0640 root:git) |
| `ct/VERSION`, `ct/RELEASE-KEY.asc`, `ct/RELEASE-KEY.fingerprint` | lus par l'**hôte** | rien — ils gouvernent ce qui est téléchargé, et ce qui est refusé |
| `ct/forgejo.service` | **CT 400** | `/etc/systemd/system/` du CT |
| `ct/fj-backup.service` / `.timer` | **CT 400** | `/etc/systemd/system/` du CT |
| `ct/10-forgejo.conf`, `ct/pg_hba.conf`, `ct/pg_ident.conf` | **CT 400** | symlinks depuis `/etc/forgejo-git` |
| `ct/init.sql` | **CT 400** | joué par `fj deploy` |
| `host/fjbk-offsite.service` / `.timer` | **hôte** | `/etc/systemd/system/` de l'hôte |

Les chemins **`/etc/forgejo-git/<fichier>`** sont stables : c'est le contrat du
montage. `app.ini` est une **copie** et non un symlink, contrairement aux
fichiers de PostgreSQL — Forgejo réécrit sa configuration s'il lui manque un
secret, et cette écriture sur un montage en lecture seule échoue d'une façon
illisible ([runbook § 7](doc/RUNBOOK.md#7-les-secrets)).

Le volume de sauvegarde porte **deux noms selon le point de vue**, et c'est la
confusion la plus facile à faire ici :

| Vu du CT | Vu de l'hôte |
|---|---|
| `/var/backups/forgejo` | `/data/subvol-400-disk-0` |

`fj backup` écrit dans le premier, `fj offsite` lit le second. Ce sont les
mêmes octets.

## En cas de pépin

**Quelque chose est perdu ?** Aller directement au
[PRA](doc/PRA.md#trouver-son-scénario) : il commence par une table de
diagnostic et donne une procédure complète par scénario.

Avant de chercher : **`fj status`** dit lequel des quatre maillons est rompu.

| Symptôme | Où regarder |
|---|---|
| `fj deploy` refuse : « ne porte aucune version » | `ct/VERSION` non résolu, [§ 4](doc/RUNBOOK.md#4-la-version-épinglée) |
| `fj deploy` refuse : « clé de publication absente » | [§ 4](doc/RUNBOOK.md#la-clé-de-publication) — geste manuel, une fois |
| `signature GPG NON vérifiée` | **ne rien installer**, [§ 4](doc/RUNBOOK.md#quand-la-vérification-échoue) |
| `Peer authentication failed for user "forgejo"` | `pg_ident.conf`, [§ 3](doc/RUNBOOK.md#3-postgresql-co-localisé) |
| Forgejo tourne mais les sessions sautent | secrets non déposés, [§ 7](doc/RUNBOOK.md#7-les-secrets) |
| Le clone SSH est refusé | routeur TCP Traefik, [§ 6](doc/RUNBOOK.md#6-routage-traefik) |
| Une socket TCP apparaît sur 5432 | drop-in non relu, [§ 3](doc/RUNBOOK.md#3-postgresql-co-localisé) |
| Après restauration, isolation disparue | les ACL ne sont pas dans le dump, [§ 9](doc/RUNBOOK.md#les-acl-ne-sont-pas-dans-le-dump) |
| `fjbk-offsite` sort en code 3 | objet distant divergent, [§ 10](doc/RUNBOOK.md#objet-distant-divergent) |
| `fjbk-offsite.timer` reste inactif | le bilan de `fj deploy` nomme le prérequis manquant |
| CT en `243/CREDENTIALS` | nesting, [§ 1](doc/RUNBOOK.md#le-piège-du-nesting) |

## Reste à faire

- [x] **Renseigner `REVERSE_PROXY_TRUSTED_PROXIES`** dans `ct/app.ini` — il
      porte encore le marqueur `@@TRAEFIK_IP@@`, et `fj deploy` refuse de
      rendre un bilan vert tant qu'il est là. C'est l'IP du CT 201.
- [x] **Résoudre `ct/VERSION`** (`fj version --resolve`) et commiter — sans
      elle, `fj deploy` n'installe rien, délibérément.
- [ ] **Épingler la clé de signature** : `fj key --fetch`, puis commiter les
      deux fichiers produits. Une minute. Ensuite, toute mise à jour dont la
      clé aurait changé est refusée
      ([§ 4](doc/RUNBOOK.md#la-clé-de-publication)).
- [ ] **Le credential ArgoCD → Forgejo doit être un Sealed Secret**, pas un
      ExternalSecret : un ExternalSecret réintroduirait la dépendance
      ESO → OpenBao au démarrage, ce qui viole le principe « Sealed Secrets
      pour ce qui doit fonctionner quand rien d'autre ne fonctionne ».
- [ ] **Push-mirror sortant vers GitHub** pour les dépôts critiques (au
      minimum les manifests ArgoCD) — [§ 11](doc/RUNBOOK.md#11-miroir-sortant-vers-github).
- [ ] **Ajouter le CTID 400 aux sauvegardes vzdump** sélectionnées vers GCS
      Nearline — les dépôts n'ont pas d'autre copie
      ([§ 9](doc/RUNBOOK.md#les-dépôts-partent-par-vzdump)).
- [ ] **Retirer la ligne `forgejo` de `pg_hba.conf` du CT 200** : la base est
      co-localisée, ce locataire n'existera jamais là-bas.
- [ ] **Jouer le premier exercice de PRA**
      ([doc/PRA-exercice.md](doc/PRA-exercice.md)) — tant qu'il ne l'a pas été,
      le RTO est inconnu et le plan n'est pas prouvé.
- [ ] Promouvoir dans `lib/` les sections A/D/F, qui existent maintenant en
      deux exemplaires (ici et dans `pgtool`) — en **déplaçant** la version
      éprouvée du CT 200, jamais en fusionnant les deux.
