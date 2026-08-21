# homelab_proxmox

Configuration et outillage des nœuds Proxmox du homelab. Un répertoire par
nœud, un sous-répertoire par service. Les conventions du dépôt sont dans
[AGENTS.md](AGENTS.md).

| Nœud | Adresse | Ce qu'il porte |
|---|---|---|
| `pve-ysera` | 192.168.1.10 | Traefik (ingress), Homepage |
| `pve-eranikus` | 192.168.1.11 | PostgreSQL mutualisé, **Forgejo** |

L'ingress et la source de vérité sont **sur deux nœuds différents**, et c'est
délibéré : la perte de `pve-ysera` coûte le routage mais laisse Forgejo
joignable en direct, et la perte de `pve-eranikus` laisse Traefik debout,
prêt à resservir dès qu'un conteneur reprend l'IP.

## Services

| Service | Nœud | CTID | Documentation |
|---|---|---|---|
| PostgreSQL mutualisé | `pve-eranikus` | 200 | [pve-eranikus/pgsql/](pve-eranikus/pgsql/README.md) |
| Forgejo — source de vérité d'ArgoCD | `pve-eranikus` | 400 | [pve-eranikus/forgejo/](pve-eranikus/forgejo/README.md) |

## Convention de numérotation des conteneurs

Le CTID dit **comment un conteneur est né, et donc comment il se met à jour**.
Ce n'est pas un rangement esthétique : c'est ce qui permet de savoir, sans
ouvrir le conteneur, si on peut lui passer un script communautaire dessus.

| Plage | Nature | Mise à jour |
|---|---|---|
| **100–199** | Jetables — essais, maquettes, tout ce qu'on détruit sans regret | Aucune. On recrée. |
| **200–299** | Posés par un **script communautaire** (`community-scripts`) | Par le script lui-même, `update` |
| **300–399** | Posés par **Terraform** | Par Terraform, en réappliquant le plan |
| **400–499** | **Installations manuelles à version épinglée** | **Jamais automatique.** Voir ci-dessous. |

### Le tier 400–499 : installations manuelles

Un conteneur de ce tier porte un logiciel installé **à la main, en binaire, à
une version choisie** — parce que la version compte plus que la fraîcheur.

Trois règles, et elles vont ensemble :

1. **Un CT de ce tier n'est JAMAIS mis à jour par un script communautaire.**
   La fonction `update_script()` de ces scripts redéploie systématiquement la
   dernière publication, sans question ni sauvegarde préalable. Sur un
   logiciel qui migre son schéma de base au démarrage, cela fait sauter une
   version majeure un matin, de façon irréversible sans restauration.

2. **La version est épinglée dans un fichier `VERSION` du dépôt**, relu à
   chaque déploiement et comparé au binaire réellement installé. Un épinglage
   qui n'est pas traçable n'est pas un épinglage : on ne peut ni le relire, ni
   voir dans `git log` quand il a bougé et pourquoi.

3. **Changer de version est une décision**, prise en lisant les notes de
   publication, et elle se commite. Elle ne se prend pas par défaut, et
   surtout pas à 3 h du matin par un timer.

Les services de ce tier documentent leur branche et sa fin de support dans
leur `README.md` et dans leur fichier `VERSION`.

| CTID | Service | Branche | Fin de support |
|---|---|---|---|
| 400 | Forgejo | 15.0 (LTS) | 15 juillet 2027 |

## Outillage

`lib/` porte les briques partagées par les deux nœuds — journalisation,
exécution, moteur de convergence, wrappers `pct`/`pvesm`/`zfs`. Chaque service
s'appuie dessus pour son propre lanceur (`pg`, `fj`). La frontière entre
`lib/core/` et `lib/proxmox/` est une frontière de sécurité, décrite dans
[AGENTS.md](AGENTS.md#lib--les-briques-partagées-par-les-deux-nœuds).

Les tests tournent sans infrastructure, sans réseau et sans conteneur :

```bash
python3 -m pytest -q
```
