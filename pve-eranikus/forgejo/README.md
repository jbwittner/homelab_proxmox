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
| Base de données | **locataire du CT 200** (`192.168.1.56`), TCP + SSL + scram |
| Ingress | Traefik (CT 201) → `forgejo.lan.wittner.tech`, SSH en 2222 |
| Base système | 32 Go minimum |
| Sauvegarde de la base | par le CT 200 — `pg backup`, `pg list`, `pgbk-offsite` |
| Sauvegarde des dépôts | par `vzdump` du CT 400 |
| Dépôt monté | `/root/homelab_proxmox/pve-eranikus/forgejo/ct` → `/etc/forgejo-git` (ro) |

> **Ce conteneur ne sauvegarde rien lui-même**, et c'est voulu. Sa base est un
> locataire du cluster mutualisé : elle est sauvegardée et copiée hors-site par
> [l'outillage du CT 200](../pgsql/README.md). Ses dépôts partent par `vzdump`.
> Deux filets pour un même objet, c'est un filet que personne ne surveille.

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

Elle installe les paquets manquants, pose le point de montage du dépôt, crée
l'utilisateur `git` et l'arborescence, **télécharge et vérifie le binaire
épinglé**, éprouve la connexion à la base du CT 200, rend `app.ini`, dépose les
secrets et arme le service. Détail :
[runbook § 2](doc/RUNBOOK.md#2-déploiement-depuis-lhôte--fj-deploy).

```bash
fj deploy --status        # état de chaque élément, ne change rien
fj deploy --dry-run       # annonce ce qui serait fait, effets compris
fj deploy --ctid 401      # cible un autre conteneur, et le consigne
fj deploy --secrets       # autorise la génération des secrets manquants
fj deploy --admin jbwittner   # crée un compte d'administration
fj deploy --no-container  # ne touche pas au CT
fj deploy --no-install    # n'installe ni paquet ni binaire (nœud sans réseau)
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

**Trois choses qu'il ne fait pas**, délibérément :

| Geste | Pourquoi il reste à part |
|---|---|
| Créer le conteneur | Geste unique — [§ 1](doc/RUNBOOK.md#1-création-du-conteneur) |
| Créer le locataire de la base | C'est `pg deploy --tenant forgejo`, sur le CT 200 — [§ 3](doc/RUNBOOK.md#3-la-base-locataire-du-ct-200) |
| Récupérer la clé de signature Forgejo | C'est `fj key --fetch`, joué une fois — [§ 4](doc/RUNBOOK.md#la-clé-de-publication) |

Les deux derniers ne sont pas des corvées. **Créer la base ailleurs qu'ici est
la seule façon de n'avoir qu'une définition de ce qu'est un locataire** : deux
outils qui créeraient la même base finiraient par la créer de deux façons, dont
une sans ses ACL. Et pour la clé, **récupérer et vérifier sont deux gestes**,
ce qui permet à `fj deploy` de n'interroger personne.

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
fj status         # les maillons du montage — À COMMENCER PAR LÀ
fj version        # ce qui est épinglé
fj key            # la clé de signature épinglée
```

**`fj status` est la commande à taper quand on se demande si tout va bien.**
Elle regarde ensemble les trois maillons qui peuvent se rompre en silence : le
service, la version réellement servie, et la base du CT 200 **vue depuis ce
conteneur-ci**. Ce dernier point compte : la base peut très bien répondre au
CT 200 lui-même et refuser celui-ci, faute d'une ligne dans son `pg_hba.conf`.

`fj deploy --status` répond à une autre question — celle de savoir si les
fichiers sont en place. Et `fj status` sort en 1 dès qu'une alarme est levée :
**un maillon non constaté est une alarme**, pas un silence.

La sauvegarde, elle, se regarde **sur le CT 200** :

```bash
pg status                          # les trois maillons de la sauvegarde
pg list                            # instantanés : âge, taille, bases
pg verify forgejo                  # ACL et propriétaires de NOTRE base
```

Journaux :

```bash
pct exec 400 -- journalctl -u forgejo -n 50 --no-pager      # le service
pct exec 400 -- journalctl -u forgejo -p warning            # anomalies seules
pct exec 200 -- journalctl -u pg-backup -n 50 --no-pager    # sauvegarde de la base
```

## Où va chaque fichier

**`fj` est un outil de NŒUD, et rien d'autre.** Il n'y a plus de `host/` dans
ce répertoire, et rien n'est poussé dans le conteneur : depuis que la base est
un locataire du CT 200, aucune commande `fj` ne s'exécute là-bas. Tout passe
par `pct exec`.

**`ct/` est la charge utile du montage** — lui seul est monté en
`/etc/forgejo-git`, en lecture seule.

| Fichier | Lu par | Installé en |
|---|---|---|
| `fj`, `fjtool/` + `lib/` (racine du dépôt) | **nœud** | `/usr/local/sbin/fj`, arbre d'import en `/usr/local/lib/fjtool` |
| `ct/app.ini` | **CT 400** | **rendu** en `/etc/forgejo/app.ini` (0640 root:git) |
| `ct/forgejo.service` | **CT 400** | copie en `/etc/systemd/system/` |
| `ct/VERSION` | **nœud** | rien — il gouverne ce qui est téléchargé |
| `ct/RELEASE-KEY.asc`, `ct/RELEASE-KEY.fingerprint` | **nœud** | rien — ils gouvernent ce qui est refusé |

Les chemins **`/etc/forgejo-git/<fichier>`** sont stables : c'est le contrat du
montage.

**`app.ini` est RENDU, pas copié.** C'est la seule exception du dépôt, et elle
tient à une valeur : le mot de passe de la base, substitué au marqueur
`@@DB_PASSWORD@@` depuis `/etc/forgejo/secrets/db_password`. Le fichier servi
ne peut donc pas exister dans le dépôt. Conséquence pratique : **un `git pull`
ne suffit jamais**, il faut rejouer `fj deploy`
([runbook § 5](doc/RUNBOOK.md#5-arborescence-et-configuration)).

## En cas de pépin

**Quelque chose est perdu ?** Aller directement au
[PRA](doc/PRA.md#trouver-son-scénario) : il commence par une table de
diagnostic et donne une procédure complète par scénario.

Avant de chercher : **`fj status`** dit lequel des trois maillons est rompu.

| Symptôme | Où regarder |
|---|---|
| `fj deploy` refuse : « ne porte aucune version » | `ct/VERSION` non résolu, [§ 4](doc/RUNBOOK.md#4-la-version-épinglée) |
| `fj key` refuse : la clé ne correspond plus | **ne rien installer**, [§ 4](doc/RUNBOOK.md#quand-la-vérification-échoue) |
| `signature GPG NON vérifiée` | **ne rien installer**, [§ 4](doc/RUNBOOK.md#quand-la-vérification-échoue) |
| `no pg_hba.conf entry for host "192.168.1.57"` | ligne manquante sur le CT 200, [§ 3](doc/RUNBOOK.md#3-la-base-locataire-du-ct-200) |
| `password authentication failed for user "forgejo"` | mot de passe déposé ≠ celui du rôle, [§ 3](doc/RUNBOOK.md#3-la-base-locataire-du-ct-200) |
| `database "forgejo" does not exist` | locataire jamais créé, [§ 3](doc/RUNBOOK.md#3-la-base-locataire-du-ct-200) |
| Forgejo tourne mais les sessions sautent | secrets non déposés, [§ 7](doc/RUNBOOK.md#7-les-secrets) |
| Le clone SSH est refusé | routeur TCP Traefik, [§ 6](doc/RUNBOOK.md#6-routage-traefik) |
| Après restauration, isolation disparue | les ACL ne sont pas dans le dump — `pg verify forgejo` sur le CT 200 |
| CT en `243/CREDENTIALS` | nesting, [§ 1](doc/RUNBOOK.md#le-piège-du-nesting) |

## Reste à faire

- [x] **`REVERSE_PROXY_TRUSTED_PROXIES` renseigné** dans `ct/app.ini` :
      `192.168.1.50`, l'IP de Traefik. Le marqueur `@@TRAEFIK_IP@@` a disparu,
      et le contrôle « proxy de confiance » ne bloque plus.
- [x] **`ct/VERSION` résolue** : `v15.0.7`, par `fj version --resolve` puis
      commit. `fj deploy` pose désormais cette version-là, et elle seule.
- [x] **Clé de signature épinglée** : `EB114F5E6C0DC2BCDD183550A4B61A2DC5923710`
      (`Forgejo Releases <release@forgejo.org>`), récupérée du WKD du projet.
      Confrontée au canal de Codeberg le 21 août 2026 : la sous-clé qui signe
      `v15.0.7` en fait bien partie
      ([§ 4](doc/RUNBOOK.md#la-confrontation-à-deux-canaux-faite-le-21-août-2026)).
- [ ] **Le credential ArgoCD → Forgejo doit être un Sealed Secret**, pas un
      ExternalSecret : un ExternalSecret réintroduirait la dépendance
      ESO → OpenBao au démarrage, ce qui viole le principe « Sealed Secrets
      pour ce qui doit fonctionner quand rien d'autre ne fonctionne ».
- [ ] **Push-mirror sortant vers GitHub** pour les dépôts critiques (au
      minimum les manifests ArgoCD) — [§ 11](doc/RUNBOOK.md#11-miroir-sortant-vers-github).
- [ ] **Ajouter le CTID 400 aux sauvegardes vzdump** sélectionnées vers GCS
      Nearline — les dépôts n'ont pas d'autre copie
      ([§ 9](doc/RUNBOOK.md#les-dépôts--à-faire-une-fois)).
- [ ] **Créer le locataire sur le CT 200** : `pg deploy --tenant forgejo`,
      ranger le mot de passe dans OpenBao, le déposer dans le CT 400, et
      vérifier que la ligne `hostssl` est bien **avant** le `reject`
      ([§ 3](doc/RUNBOOK.md#3-la-base-locataire-du-ct-200)). Sans ça,
      `fj deploy` refuse au maillon « connexion à la base ».
- [ ] **Jouer le premier exercice de PRA**
      ([doc/PRA-exercice.md](doc/PRA-exercice.md)) — tant qu'il ne l'a pas été,
      le RTO est inconnu et le plan n'est pas prouvé.
- [ ] Promouvoir dans `lib/` les sections A/D/F, qui existent maintenant en
      deux exemplaires (ici et dans `pgtool`) — en **déplaçant** la version
      éprouvée du CT 200, jamais en fusionnant les deux.
