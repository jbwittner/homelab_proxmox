# Forgejo — VM Docker

Instance Forgejo à version épinglée, **source de vérité GitOps** : ArgoCD y
pointe. Elle doit rester disponible et réconciliable **quand le cluster
Kubernetes est indisponible**. C'est la contrainte qui prime sur toutes les
autres, et elle explique chacune des décisions surprenantes de ce répertoire —
à commencer par le fait que la base vive dans la même machine.

Ce fichier ne porte que **ce qu'on tape**. Le reste est dans `doc/` :

| | |
|---|---|
| [doc/RUNBOOK.md](doc/RUNBOOK.md) | le détail — création de la VM, conception, pièges rencontrés |
| [doc/PRA.md](doc/PRA.md) | **les mauvais jours** — une procédure complète par scénario |
| [doc/PRA-exercice.md](doc/PRA-exercice.md) | jouer le PRA pour de faux, et mesurer le RTO |

## Fiche d'identité

| | |
|---|---|
| VMID | **300**, hostname `forgejo` |
| IP | `192.168.1.56/24`, passerelle `192.168.1.254` |
| Nœud | `pve-eranikus` (192.168.1.11), Debian 13 (`genericcloud`) |
| Ressources | 2 vCPU, 4 Go **fixes** (`balloon 0`), disque système 20 Go (`local-lvm`) |
| Données | **40 Go**, `LABEL=srv` → `/srv/forgejo` — dépôts, LFS, pièces jointes **et** la base, sur le même volume. Sauvegardé. [dimensionnement](doc/RUNBOOK.md#dimensionner-les-trois-volumes) |
| Artefacts | **100 Go**, `LABEL=artifacts` → `/srv/artifacts` — le registre. **Sauvegardé, et c'est une décision** — [pourquoi](doc/RUNBOOK.md#le-cas-des-artefacts) |
| Sauvegardes | **50 Go**, `LABEL=backup` → `/srv/backup`, `backup=0` — les 7 dernières paires, avant leur envoi vers GCS |
| Forgejo | **15.0.7**, branche **15.0 LTS**, fin de support **15 juillet 2027** |
| Base | PostgreSQL 18, **dans la même pile**, volume `/srv/forgejo/db` |
| Ingress | Traefik (CT 201, `pve-ysera`) → `https://forgejo.wittner.tech/`, SSH en 2222 |
| Sauvegarde | `fjbk backup`, toutes les nuits à 3 h, paire base + dépôts vers GCS |
| Le dépôt, dans la VM | `/opt/homelab` |

> **Quatre disques, quatre cycles de vie**, et aucun monté sous un autre :
> remplir l'un ne peut pas arrêter les autres. Le registre, lui, **est
> sauvegardé — et c'est l'inverse de la décision précédente** : « ça se
> reconstruit depuis le code » suppose une CI disponible, or la CI a vocation à
> vivre ici et ArgoCD tire ses images du registre. Hors de la paire nocturne,
> mais dans le vzdump — donc il survit au [scénario 2](doc/PRA.md#2--vm-cassée)
> et pas au [3](doc/PRA.md#3--nœud-perdu--sinistre) :
> [Le cas des artefacts](doc/RUNBOOK.md#le-cas-des-artefacts).

> **Tout est dans une seule machine, et c'est le point.** L'ancien montage
> séparait la base (locataire d'un cluster mutualisé) des dépôts (un CT à
> binaire épinglé) : la reprise se faisait en deux temps, sur deux machines,
> dans le bon ordre, et rien ne garantissait que les deux moitiés se
> recouvraient. Ici, **une sauvegarde est une paire cohérente et une
> restauration est un geste unique**.

> **Cette VM ne se met jamais à jour toute seule.** Ni Forgejo, ni le système
> au-delà des correctifs de sécurité. Passer en 16 ou 17 est une décision qui se
> prend en lisant les notes de publication et qui se commite —
> [runbook § 5](doc/RUNBOOK.md#5-mettre-à-jour-forgejo).

## Gestes courants

Tout se fait depuis `/opt/homelab/pve-eranikus/forgejo` dans la VM.

```bash
ssh admin@192.168.1.56
cd /opt/homelab/pve-eranikus/forgejo
```

| Ce qu'on veut | Ce qu'on tape |
|---|---|
| L'état de la pile | `./scripts/fj-check.py` — six contrôles, 0 ou 1 |
| Idem, pour une machine | `./scripts/fj-check.py --json` |
| Les journaux | `docker compose logs -f forgejo` |
| Redémarrer | `docker compose restart forgejo` |
| Sauvegarder maintenant | `sudo ./scripts/fjbk backup` |
| Voir les sauvegardes | `sudo ./scripts/fjbk list` |
| Éprouver une paire | `sudo ./scripts/fjbk verify <horodatage>` |
| **Restaurer** | `sudo ./scripts/fjbk restore <horodatage>` — demande de retaper l'horodatage |
| Mettre à jour le système | `sudo ./scripts/sys-update.sh` — ne redémarre jamais |
| Créer un compte | `docker compose exec -u git forgejo forgejo admin user create --admin --username <nom> --email <mail>` |

Les inscriptions sont fermées et rien n'est lisible sans compte : les comptes se
créent à la main, avec la commande ci-dessus.

## Symptôme → où regarder

| Symptôme | Où regarder |
|---|---|
| Le site ne répond pas, la VM répond en SSH | `./scripts/fj-check.py`, puis [PRA § 1](doc/PRA.md#1--forgejo-indisponible-vm-saine) |
| `502` depuis Traefik | la pile est-elle debout ? `docker compose ps` |
| Une connexion réussie renvoie vers `http://192.168.1.56:3000/` | `passHostHeader` — [runbook § 9](doc/RUNBOOK.md#9-pièges-rencontrés) |
| `ssh: connect to host … port 2222: Connection refused` | l'entryPoint `ssh` de Traefik est **statique** : redémarrer Traefik |
| `Permission denied (publickey)` au `git pull` de la VM | la clé de déploiement n'est pas déclarée, ou le remote vise le port 22 — [runbook § 4](doc/RUNBOOK.md#le-clone-passe-par-le-port-2222-jamais-par-22) |
| `required variable FORGEJO_… is missing a value` | le `.env` a disparu — [PRA § 1, cas C](doc/PRA.md#cas-c--forgejo-boucle-au-démarrage) |
| Les miroirs push échouent sans message | `SECRET_KEY` n'est pas celui d'origine — [runbook § 9](doc/RUNBOOK.md#9-pièges-rencontrés) |
| La base démarre vide après une reconstruction | `PGDATA` — [runbook § 9](doc/RUNBOOK.md#9-pièges-rencontrés) |
| L'unité `fjbk.service` est en échec | le code de retour dit lequel — [runbook § 7](doc/RUNBOOK.md#7-la-sauvegarde) |
| `fjbk` sort en 3 | `fj-check.py` est rouge : [doc/PRA.md](doc/PRA.md) |
| Un disque est plein | `df -h / /srv/forgejo /srv/artifacts /srv/backup` — lequel change la réponse : [PRA § 1, cas D](doc/PRA.md#cas-d--le-disque-est-plein) |
| La racine se remplit sans raison | un volume n'est pas monté : son contenu s'écrit sur le disque système de 20 Go — [PRA § 1, cas E](doc/PRA.md#cas-e--fj-checkpy-dit-quun-volume-nest-pas-monté) |
| Après une reprise sur un **autre nœud**, le registre est vide | **c'est attendu** : il est sauvegardé par le vzdump, resté sur le nœud perdu — [pourquoi](doc/RUNBOOK.md#le-cas-des-artefacts) |

## Où va chaque fichier

| | |
|---|---|
| `compose.yaml` | les deux services, toute la configuration en `FORGEJO__section__CLE`. **Pas d'`app.ini` versionné** : c'est le fichier que Forgejo réécrit dès qu'un secret lui manque. |
| `env.example` | les clés attendues du `.env`, **aucune valeur** |
| `.env` | **jamais versionné.** Chiffré par sops sur le poste, déposé par `scp` — jamais par `git pull`, la clé age reste sur le poste. |
| `scripts/init.sh` | provisionnement d'une VM neuve, **une seule fois**. Ne formate jamais. Produit la clé de déploiement et affiche sa partie publique. |
| `scripts/sys-update.sh` | `dist-upgrade`, signale le redémarrage sans le faire |
| `scripts/fjbk` | sauvegarde et restauration. 300 lignes, un fichier, pas de moteur. |
| `scripts/fj-check.py` | santé de la pile, `0`/`1`, `--json` |
| `scripts/fjbk.service` / `.timer` | l'automatisme, 3 h du matin, `Persistent=true` |
| `/etc/default/fjbk` | **hors dépôt** — le bucket et la rétention, propres à la machine |
| `~admin/.ssh/id_ed25519` | **hors dépôt, née dans la VM et n'en sort pas** — la clé de déploiement, déclarée en **lecture seule** côté Forgejo |

## La boucle assumée

Ce dépôt est cloné dans la VM **depuis Forgejo lui-même** — circularité assumée,
et donc écrite : [runbook § 8](doc/RUNBOOK.md#8-la-boucle-assumée). Ses deux
sorties sont le **clone local sur le poste** et le **push mirror vers GitHub**.
Le jour où Forgejo est mort et qu'on a besoin du dépôt, on ne fait pas
`git pull` : on joue [PRA § 4](doc/PRA.md#4--forgejo-est-mort-et-jai-besoin-du-dépôt).

## Reste à faire

- [ ] Créer le bucket GCS dédié et son compte de service
      (`objectViewer` + `objectCreator`, **ni écrasement ni suppression**),
      puis renseigner `FJBK_BUCKET` dans `/etc/default/fjbk`.
- [ ] Poser la règle de cycle de vie du bucket — **c'est là, et nulle part dans
      le code, que vit la rétention distante.**
- [ ] Créer le dépôt miroir sur GitHub et configurer le push mirror.
- [ ] **Jouer le PRA « nœud perdu » et reporter le RTO** —
      [doc/PRA-exercice.md](doc/PRA-exercice.md). Tant que ce n'est pas fait, le
      RTO du PRA est vide, et c'est volontaire.
- [ ] Supprimer la clé de déploiement GitHub une fois la bascule faite
      ([runbook § 8](doc/RUNBOOK.md#8-la-boucle-assumée)).
- [ ] **Répliquer les vzdump hors du nœud** — moitié manquante de la décision
      de sauvegarder le registre : le vzdump en est le seul support, et il
      disparaît avec le nœud ([pourquoi](doc/RUNBOOK.md#le-cas-des-artefacts)).
- [ ] **La politique de rétention du registre**, le jour où la CI publie :
      `/srv/artifacts` est le seul volume que rien ne fait décroître.
- [ ] Dimensionner les volumes pour de bon. 40 / 100 / 50 Go sont des points de
      départ, et chacun s'agrandit en ligne — `qm disk resize 300 scsi2 +100G`
      puis `resize2fs "$(findmnt -no SOURCE /srv/artifacts)"`. C'est **le disque
      des sauvegardes** qui plafonne les dépôts, pas le leur
      ([le calcul](doc/RUNBOOK.md#dimensionner-les-trois-volumes)).
