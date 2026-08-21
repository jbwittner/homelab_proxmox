# Conventions du dépôt

Homelab Proxmox. Un répertoire par nœud, un sous-répertoire par service.

**Le contrat du montage, c'est `/etc/<service>-git/<fichier>`.** Ce que le
conteneur monte vit à plat dans **`ct/`**, et ces chemins-là ne bougent jamais.
Ce qui s'installe sur le nœud vit dans **`host/`** — le conteneur n'a aucune
raison de voir les scripts de l'hôte ni la configuration d'un stockage distant.
Le déployeur et la documentation restent à la racine du service, et **la
documentation va dans `doc/`**.

Le critère de découpage n'est pas « quelle machine l'exécute » mais **« est-ce la
charge utile du montage »** : un fichier qui tourne des deux côtés vit dans `ct/`,
et l'hôte le lit à travers la frontière. `ct/` est une frontière de *visibilité*,
pas d'exécution.

Ces règles viennent de ce qui a été construit ici. Les suivre, et les étendre
quand un nouveau service apparaît.

## `lib/` — les briques partagées par les deux nœuds

Un service qui a besoin de plus qu'un script bash s'appuie sur `lib/`, à la
racine du dépôt. Deux paquets, et la frontière entre eux est une frontière de
sécurité, pas de goût :

| | |
|---|---|
| `lib/core/` | générique : journalisation, exécution, wrappers d'outils. **Poussé DANS les conteneurs.** N'importe jamais `lib/proxmox/`, ne nomme aucun service. |
| `lib/proxmox/` | `pct`, `pvesm`, `zfs`. **Ne quitte jamais l'hôte** — un conteneur n'a rien à faire avec `pct`. |

- **Bibliothèque standard uniquement.** Aucun `pip install` sur l'hyperviseur
  ni dans un conteneur. `pytest` est admis en développement, jamais importé par
  le code livré.
- **Jamais de chaîne shell.** Tout est un argv passé à `subprocess` : le triple
  échappement Python → `pct` → shell du conteneur n'existe pas parce qu'aucun
  shell n'intervient. Ni `shell=True`, ni commande construite par concaténation.
- **Lecture et écriture sont distinctes dans la signature.** `read()` s'exécute
  toujours, `write()` est neutralisée en simulation : un contrôle qui ne
  pourrait plus lire l'état n'aurait rien à comparer.
- **Les pièges s'encodent dans les types**, pas dans des commentaires. Poser un
  point de montage renvoie « faut-il redémarrer » ; créer un pool ZFS sur un
  chemin instable lève ; un mot de passe est un `Secret`, qui ne peut pas
  ressortir dans un journal.
- **Le conteneur reçoit `core/` par `pct push`**, jamais par un second point
  de montage : le montage est vivant, et un `git pull` en pleine nuit livrerait
  un arbre à moitié à jour.

`tests/` à la racine, `pytest`, **sans infrastructure** : tout passe par des
doubles. Un double doit tenir compte de ses propres écritures — un faux nœud
dont `pct config` ignore les `pct set` valide n'importe quoi. Les règles
ci-dessus sont elles-mêmes vérifiées par des tests : une convention qu'aucun
test ne défend se perd à la troisième modification.

## Documentation : un README court, un runbook détaillé

Jamais un seul gros fichier. Un README court à la racine du service, le reste
dans `doc/` :

| Fichier | Contenu | Cible |
|---|---|---|
| `README.md` | **ce qu'on tape** — fiche d'identité, la commande de déploiement, les gestes courants, où va chaque fichier, une table « symptôme → où regarder », le reste à faire | ~150 lignes, pas plus |
| `doc/RUNBOOK.md` | le détail — création, conception, pièges rencontrés en production, procédures rares | aussi long qu'il faut |
| `doc/PRA.md` | **une procédure de reprise par scénario**, du dégât local à la perte du nœud | une entrée par scénario |
| `doc/PRA-exercice.md` | comment jouer le PRA pour de faux, et mesurer le RTO | cases à cocher et champs à remplir |

- Sections **numérotées et stables** dans le runbook : les scripts y renvoient
  par numéro dans leurs messages d'erreur (« voir doc/RUNBOOK.md section 10 »),
  et un `Documentation=` d'unité systemd pointe dessus. Déplacer un document,
  c'est aussi corriger ces renvois-là.
- Sommaire cliquable en tête du runbook, liens ancrés depuis le README.
- Un piège rencontré en production se documente **avec sa date** et le message
  d'erreur exact — c'est ce qui le rend reconnaissable la fois suivante.
- Le README décrit ce que **fait le script**, pas une suite de commandes à
  retaper. Les commandes équivalentes vont dans le runbook, pour comprendre et
  rejouer à la main.

### Le PRA a ses propres règles

- **Un scénario, une procédure complète.** Le PRA se répète volontairement :
  en reprise on ne lit pas un document, on va à son cas et on doit y trouver
  tout ce qu'il faut sans naviguer. C'est le seul endroit où la duplication
  est un choix.
- Il commence par une **table de diagnostic** — ce qu'on constate → le
  scénario — et non par une explication.
- Il annonce le **RPO** franchement, et laisse le **RTO vide tant qu'il n'a
  pas été mesuré** par un exercice. Une durée estimée de tête n'a aucune
  valeur le jour où on en a besoin.
- L'exercice s'écrit avec des **cases à cocher, des champs de mesure et un
  journal** à remplir. Il liste ses **garde-fous** en tête — ce qui, joué
  distraitement, casserait la production — et se termine par un démontage
  dont aucune étape n'est facultative.
- Il dit aussi **ce qu'il ne couvre pas**, pour qu'un exercice réussi ne se
  confonde pas avec « on est couvert ».

## Scripts utilitaires : un point d'entrée unique

Le réflexe par défaut est d'**écrire un script**, pas d'écrire une procédure.

- **Un seul script fait tout** : paquets, points de montage, configuration,
  unités systemd, premier lancement. On tape une commande, on obtient un
  service qui tourne.
- **Rejouable à l'identique.** Première pose et mise à jour sont la même
  commande ; chaque étape est conditionnelle et ne touche à rien si l'état est
  déjà conforme.
- `--dry-run` et `--status` sont obligatoires. Sur un état conforme,
  `--dry-run` doit annoncer **zéro modification** : c'est le contrôle qui
  prouve que le script décrit l'état existant et non un état voisin.
- **Ce qui reste manuel doit être justifié et signalé par le script lui-même**
  (un secret, une création interactive). Le script dit quoi faire et où.
- **Résumé final**, une ligne par élément : `OK` / `POSE` / `KO`.

### Forme

- `set -Eeuo pipefail` en tête.
- **Chemins absolus partout** : `pct exec` et les unités systemd fournissent un
  `PATH` minimal qui n'inclut pas `/usr/local/bin`.
- Journalisation horodatée et préfixée : `[STEP ]`, `[INFO ]`, `[WARN ]`,
  `[ERROR]`. `trap` sur `ERR` consignant la ligne fautive et le code de retour.
- Paramétrage par variables d'environnement avec valeurs par défaut
  (`: "${VAR:=defaut}"`), valeurs réelles dans l'unité systemd — et un drop-in
  généré quand la valeur dépend de la machine, plutôt qu'une valeur devinée.
- **Commentaires en français, expliquant le _pourquoi_** et non le _quoi_.
- L'aide (`--help`) se lit dans l'en-tête du fichier (`awk` sur les `#`).

### Comportement

- **Échouer bruyamment**, avec un message qui dit quoi faire, et un code de
  retour distinct par famille de panne. Un échec silencieux est pire qu'une
  absence de sauvegarde.
- **Ne jamais armer un automatisme dont les prérequis manquent** : poser les
  fichiers, laisser le timer inactif, le dire. Un timer qui échoue toutes les
  nuits à 3h30 n'aide personne.
- **Les opérations qui génèrent un secret** sont derrière un drapeau explicite,
  jamais jouées par défaut, et ne font rien si l'objet existe déjà — rejouer un
  déploiement ne doit pas invalider un mot de passe déjà rangé dans OpenBao.
- Ne pas réécrire un fichier hors dépôt qu'on n'a pas écrit : le créer s'il
  manque, signaler ce qui lui manque sinon.

## Prudence

- **Aucun secret dans le dépôt**, entrée `.gitignore` à l'appui.
- Vers un stockage distant : `copy`, **jamais `sync`** — `sync` réplique les
  suppressions. Aucune opération d'écrasement ni de suppression distante.
- **Ne jamais proposer un test destructif** pour vérifier une protection : elle
  se lit dans le code et dans les droits, elle ne s'éprouve pas en effaçant.
- **Distinguer hôte et conteneur explicitement** : en-tête de chaque script, et
  tableau dans le README. Un fichier posé du mauvais côté ne produit pas
  d'erreur immédiate, juste une sauvegarde qui ne part jamais.

## Vérifier sans accès à l'infrastructure

Le travail se fait dans le dépôt ; l'utilisateur installe et teste lui-même.
Donc : ne rien exécuter sur l'infrastructure, ne rien inventer sur son état,
demander l'information qui manque.

Ce qui reste vérifiable, et qui doit l'être avant de rendre :

- `bash -n` sur chaque script.
- Un **banc d'essai à stubs** dans le scratchpad — `pct`, `pvesm`, `systemctl`,
  `apt-get`, `rclone` bouchonnés, chemins absolus préfixés — pour rejouer les
  scénarios qui comptent : pose sur un état vierge, rejeu idempotent,
  `--dry-run`, et **chaque cas de refus**. C'est ce qui a trouvé les vrais bugs
  (un `awk -F': *'` qui coupait un `volid` sur son deux-points, un timer armé
  sur un volume incertain).
- **La documentation est un jeu de tests de la ligne de commande.** Extraire
  mécaniquement des documents les liens, les chemins du dépôt, les
  sous-commandes et les drapeaux, puis les confronter au parseur et à l'arbre
  réels. Ce contrôle a trouvé que `pg deploy --ctid 299` — la forme écrite
  partout, et celle dont l'exercice de PRA dépend — n'existait pas : `--ctid`
  n'avait été posé que sur le parseur global. Une relecture à l'œil ne l'aurait
  pas vu, parce qu'on lit ce qu'on croit avoir écrit.

### Un défaut se reproduit AVANT d'être corrigé

Dès qu'un défaut est constaté — en production, pendant une campagne sur le
nœud, ou en relisant — l'ordre est **toujours** :

1. écrire le test qui le reproduit, avec la **donnée exacte observée** : la
   ligne de journal, le code de retour, la sortie brute ;
2. lancer la suite et **le voir échouer** ;
3. corriger ;
4. le revoir passer.

Jamais l'inverse. Un test écrit après le correctif n'a jamais échoué : rien ne
prouve qu'il discrimine quoi que ce soit. Il peut passer pour une raison sans
rapport avec le défaut, et donner l'illusion d'une protection le jour où la
régression revient. **Voir le rouge est la seule preuve que le test parle du bon
phénomène.**

Constater l'échec après coup, en revenant en arrière avec `git checkout`, est un
rattrapage — pas l'ordre correct. Le message de commit dit les deux : ce qui a
été constaté, et que le test a été vu rouge d'abord.

Vaut aussi pour les défauts trouvés en relisant, pas seulement pour ceux
remontés par l'utilisateur.

### Une campagne sur le nœud trouve ce que les stubs ne voient pas

Les tests automatiques prouvent des décisions de code ; le banc à stubs prouve
l'idempotence. Ni l'un ni l'autre ne regarde ce que la commande **affiche
vraiment** ni le code qu'elle rend au shell. D'où une campagne de commandes
réelles sur le nœud, **non destructive** — une protection se lit dans le refus
qu'elle produit, jamais en effaçant.

Trois défauts du 21 août 2026, invisibles autrement :

| Constat | Pourquoi ça comptait |
|---|---|
| `pg offsite --foo` sortait en **2** | 2 veut dire « transfert en échec » dans la table : une faute de frappe se serait lue comme une panne de copie |
| `09:20:34 [ERROR] 09:20:34 [ERROR] …` | le refus du moteur, repassé par la journalisation de la façade, était préfixé deux fois |
| `terminé` au lieu de `terminé en 2s` | la durée avait disparu du verdict : une copie qui passe de 2 s à 40 min ne se voit plus |
