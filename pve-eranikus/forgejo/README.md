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
| Ressources | 2 vCPU, 4 Go, disque système 20 Go (`local-lvm`) |
| Données | **disque 80 Go** sur le pool ZFS `data`, monté sur `/srv` par `LABEL=srv` — [dimensionnement](doc/RUNBOOK.md#dimensionner-srv) |
| Artefacts | **disque 200 Go dédié**, `LABEL=packages` → `/srv/packages`, `backup=0`. **Non sauvegardé, c'est une décision** — [pourquoi](doc/RUNBOOK.md#le-cas-des-artefacts) |
| Forgejo | **15.0.7**, branche **15.0 LTS**, fin de support **15 juillet 2027** |
| Base | PostgreSQL 18, **dans la même pile**, volume `/srv/forgejo/db` |
| Ingress | Traefik (CT 201, `pve-ysera`) → `https://forgejo.wittner.tech/`, SSH en 2222 |
| Sauvegarde | `fjbk backup`, toutes les nuits à 3 h, paire base + dépôts vers GCS |
| Le dépôt, dans la VM | `/opt/homelab` |

> **Le registre d'artefacts n'est pas sauvegardé, et c'est délibéré.** Images
> OCI, paquets Java, npm, Go : ils se reconstruisent depuis le code, et le code
> est ce qu'on sauvegarde. Après une reprise, le registre repart vide et les
> images se republient. Trois mécanismes tiennent cette décision plutôt qu'une
> intention — [Le cas des artefacts](doc/RUNBOOK.md#le-cas-des-artefacts).

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

Tout se fait depuis `/opt/homelab/forgejo` dans la VM.

```bash
ssh admin@192.168.1.56
cd /opt/homelab/forgejo
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
| `required variable FORGEJO_… is missing a value` | le `.env` a disparu — [PRA § 1, cas C](doc/PRA.md#cas-c--forgejo-boucle-au-démarrage) |
| Les miroirs push échouent sans message | `SECRET_KEY` n'est pas celui d'origine — [runbook § 9](doc/RUNBOOK.md#9-pièges-rencontrés) |
| La base démarre vide après une reconstruction | `PGDATA` — [runbook § 9](doc/RUNBOOK.md#9-pièges-rencontrés) |
| L'unité `fjbk.service` est en échec | le code de retour dit lequel — [runbook § 7](doc/RUNBOOK.md#7-la-sauvegarde) |
| `fjbk` sort en 3 | `fj-check.py` est rouge : [doc/PRA.md](doc/PRA.md) |
| Le disque est plein | `df -h /srv` puis `fjbk list` — la purge locale garde 7 jours, et [le dimensionnement](doc/RUNBOOK.md#dimensionner-srv) explique pourquoi c'est le terme dominant |
| `/srv` se remplit sans raison | le disque du registre n'est pas monté : les artefacts s'entassent sur le volume des dépôts — [PRA § 1, cas E](doc/PRA.md#cas-e--fj-checkpy-dit-que-le-registre-nest-pas-monté) |
| Après une reprise, le registre est vide | **c'est attendu** : les artefacts ne sont pas sauvegardés, ils se republient — [pourquoi](doc/RUNBOOK.md#le-cas-des-artefacts) |

## Où va chaque fichier

| | |
|---|---|
| `compose.yaml` | les deux services, toute la configuration en `FORGEJO__section__CLE`. **Pas d'`app.ini` versionné** : c'est le fichier que Forgejo réécrit dès qu'un secret lui manque. |
| `env.example` | les clés attendues du `.env`, **aucune valeur** |
| `.env` | **jamais versionné.** Chiffré par sops sur le poste, déposé par `scp` — jamais par `git pull`, la clé age reste sur le poste. |
| `scripts/init.sh` | provisionnement d'une VM neuve, **une seule fois**. Ne formate jamais. |
| `scripts/sys-update.sh` | `dist-upgrade`, signale le redémarrage sans le faire |
| `scripts/fjbk` | sauvegarde et restauration. 300 lignes, un fichier, pas de moteur. |
| `scripts/fj-check.py` | santé de la pile, `0`/`1`, `--json` |
| `scripts/fjbk.service` / `.timer` | l'automatisme, 3 h du matin, `Persistent=true` |
| `/etc/default/fjbk` | **hors dépôt** — le bucket et la rétention, propres à la machine |

## La boucle assumée

Ce dépôt est cloné dans la VM **depuis Forgejo lui-même**. C'est une
circularité, elle est assumée, et elle est écrite noir sur blanc dans
[runbook § 8](doc/RUNBOOK.md#8-la-boucle-assumée) — une boucle assumée qui n'est
écrite nulle part redevient une boucle subie.

Ses deux sorties : **le clone local sur le poste** et **le push mirror vers
GitHub**, actif en permanence. Le jour où Forgejo est mort et qu'on a besoin du
dépôt, on ne fait pas `git pull` : on joue
[PRA § 4](doc/PRA.md#4--forgejo-est-mort-et-jai-besoin-du-dépôt).

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
- [ ] Dimensionner le disque du registre pour de bon. 200 Go est un point de
      départ : il s'agrandit en ligne (`qm disk resize 300 scsi2 +100G` puis
      `resize2fs /dev/sdc`), et rien n'en dépend puisqu'il n'est pas sauvegardé.
