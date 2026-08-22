# homelab_proxmox

Configuration et outillage des nœuds Proxmox du homelab. Les conventions du
dépôt sont dans [AGENTS.md](AGENTS.md).

| Nœud | Adresse | Ce qu'il porte |
|---|---|---|
| `pve-ysera` | 192.168.1.10 | Traefik (ingress), Homepage |
| `pve-eranikus` | 192.168.1.11 | **Forgejo** (VM 300) |

L'ingress et la source de vérité sont **sur deux nœuds différents**, et c'est
délibéré : la perte de `pve-ysera` coûte le routage mais laisse Forgejo
joignable en direct, et la perte de `pve-eranikus` laisse Traefik debout, prêt à
resservir dès qu'une machine reprend l'IP.

## Services

| Service | Où | Documentation |
|---|---|---|
| Forgejo — source de vérité d'ArgoCD | VM 300, `pve-eranikus` | [forgejo/](forgejo/README.md) |
| Traefik, Homepage | CT, `pve-ysera` | [pve-ysera/](pve-ysera/) |

**Forgejo est à la racine, et pas sous un répertoire de nœud** : c'est une VM
autonome, avec sa base dans sa propre pile. Le nœud qui l'héberge est un détail
d'implantation, et la reprise après sinistre consiste précisément à en changer.

Les LXC, eux, restent rangés par nœud : ils sont du système, ils tiennent au
nœud, et ils ne migrent pas.

## Ce qui a été retiré, et pourquoi

Le dépôt portait un moteur de convergence maison — `lib/`, `fjtool`, `pgtool`,
et leurs tests : **19 000 lignes de Python pour deux services**. Il a été
supprimé en entier. Le coût d'entretien avait dépassé le service rendu, et
aucune de ces lignes n'avait jamais éprouvé une reprise. Ce qui reste tient en
quatre scripts d'un fichier chacun, sous 300 lignes, et l'épreuve est le PRA
joué sur une VM jetable. Le raisonnement complet est dans
[AGENTS.md](AGENTS.md#les-règles-de-code).

Le **cluster PostgreSQL mutualisé (CT 200)** a disparu avec : il n'avait qu'un
seul locataire, Forgejo, et sa seule conséquence pratique était une restauration
en deux temps sur deux machines. Il reviendra le jour où un second vrai
locataire existera, pas avant. Son adresse `192.168.1.56` a été reprise par la
VM Forgejo.

## Convention de numérotation

Le numéro dit **comment une machine est née, et donc comment elle se met à
jour**. Ce n'est pas un rangement esthétique : c'est ce qui permet de savoir,
sans l'ouvrir, ce qu'on a le droit de lui passer dessus.

| Plage | Nature | Mise à jour |
|---|---|---|
| **100–199** | Jetables — essais, maquettes, exercices de PRA, tout ce qu'on détruit sans regret | Aucune. On recrée. |
| **200–299** | Conteneurs système (reverse proxy, MQTT, DNS) | Par leur propre outillage |
| **300–399** | **VM applicatives, une par service, image officielle épinglée** | `docker compose pull` après un changement de tag **commité**, et un `qm snapshot` pris avant |

Les anciens tiers 200 (scripts communautaires), 300 (Terraform) et 400
(installations manuelles épinglées) n'existent plus. Il ne restait rien du
premier, Terraform sur Proxmox est un chantier séparé qui se fera pour tout le
parc ou pour rien, et le troisième a été remplacé par la VM Docker.

| VMID / CTID | Service | Version | Fin de support |
|---|---|---|---|
| 300 | Forgejo | 15.0.7 (branche 15.0 LTS) | 15 juillet 2027 |

**Une VM applicative ne se met jamais à jour toute seule.** Ni par timer, ni par
`latest`, ni par un tag de branche qui flotte. La version est épinglée au
correctif dans le `compose.yaml`, et en changer est une décision qui se prend en
lisant les notes de publication et qui se commite — un logiciel qui migre son
schéma de base au démarrage ne se défait pas.

Les correctifs **de sécurité du système** passent seuls, par
`unattended-upgrades` restreint à l'origine `Debian-Security` et sans
redémarrage automatique. Tout le reste attend qu'on le décide.

## Outillage

Il n'y a plus de bibliothèque partagée, et c'est volontaire. Chaque service
porte ses propres scripts, **un fichier chacun**, sans dépendance hors de la
bibliothèque standard :

| | |
|---|---|
| [`forgejo/scripts/`](forgejo/scripts/) | `init.sh`, `sys-update.sh`, `fjbk`, `fj-check.py` |
| [`script/`](script/) | utilitaires de nœud, indépendants d'un service |

Il n'y a **pas de suite de tests** à lancer. Ce qui se vérifie sans
infrastructure — `bash -n`, `py_compile`, `docker compose config`, les liens de
la documentation, les codes de retour — est listé dans
[AGENTS.md](AGENTS.md#3-pas-de-tests-unitaires-sur-ces-scripts). Ce qui compte
vraiment se mesure : [le PRA joué sur une VM
jetable](forgejo/doc/PRA-exercice.md).
