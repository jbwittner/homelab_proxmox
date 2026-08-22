# Conventions du dépôt

Homelab Proxmox. **Un répertoire par nœud, un sous-répertoire par service** —
VM comme conteneurs. Un service qui change de nœud change de répertoire : c'est
un `git mv`, et cela vaut mieux qu'une exception permanente au rangement.

## Les règles de code

Elles sont la leçon d'une dérive, et il faut la nommer pour qu'elle ne
recommence pas : `fjtool` et `pgtool` avaient atteint **3 500 et 3 000 lignes
pour deux services**, plus 1 900 lignes de `lib/` partagée et 8 000 lignes de
tests. Un moteur de convergence maison, avec calcul de plan, calcul d'état, mode
simulation et étapes idempotentes — pour poser un binaire et un cluster
PostgreSQL à un seul locataire. Le coût d'entretien avait dépassé le service
rendu, et aucune de ces 19 000 lignes n'avait jamais éprouvé une reprise.

### 1. Un fichier par script

**Jamais de paquet, jamais de `lib/`, jamais de classe de base partagée.** Si
deux scripts ont besoin de la même fonction, **on la recopie**.

Deux copies d'une fonction de vingt lignes coûtent moins cher qu'une abstraction
qu'il faut comprendre avant de toucher à l'un ou à l'autre. La duplication se
voit ; le couplage, non.

### 2. Aucun moteur

**Aucun mode simulation, aucun moteur de convergence, aucun calcul de plan,
aucun calcul d'état.** Les scripts **font**, ou **échouent avec un code de
retour qui a un sens**.

Un `--dry-run` est un second programme à écrire, à tenir à jour et à croire sur
parole. Un `--status` est une troisième définition de « conforme », qui finit
par diverger des deux autres.

Un script de provisionnement pose un système **une fois** et refuse de repartir
— un témoin daté sur disque suffit. Il n'a pas à être rejouable à l'identique :
ce qu'on recrée, c'est la machine, pas l'état d'une machine.

### 3. Pas de tests unitaires sur ces scripts

**L'épreuve, c'est le PRA joué sur une VM jetable, et le RTO mesuré.**

Un double de `pct` ou de `docker` prouve qu'une fonction se comporte comme sa
spécification. Il ne dit rien de ce qu'on veut savoir : est-ce que la reprise
fonctionne, et combien de temps elle prend.

Ce qui reste vérifiable sans infrastructure, et qui doit l'être avant de rendre :

- `bash -n` sur chaque script shell, `py_compile` sur chaque script Python.
- `docker compose config` sur chaque `compose.yaml`, avec un `.env` factice.
- **La documentation est un jeu de tests.** Extraire mécaniquement des documents
  les liens et les ancres, puis les confronter aux fichiers et aux titres réels.
  Une relecture à l'œil ne les voit pas, parce qu'on lit ce qu'on croit avoir
  écrit.
- Les **codes de retour**, un par un, y compris ceux des refus. C'est ce
  contrôle qui a trouvé que `fjbk backup --foo` sortait en 2, code réservé à
  « opération en échec » — une faute de frappe se serait lue comme une
  sauvegarde ratée.

### 4. Plafond de 300 lignes par script

Un script qui le dépasse, ou qui réclame un second fichier, **est le signal de
passer à Ansible**. Pas de refactoring maison, pas d'abstraction de secours, pas
de paquet qui commence par deux modules.

Le plafond n'est pas une esthétique : c'est le seuil au-delà duquel écrire son
propre outillage coûte plus cher que d'en adopter un.

### 5. Un service applicatif va dans une VM Docker

**Image officielle, un `compose.yaml` par service, une VM par service**, rangée
sous son nœud comme le reste. La version est **épinglée au correctif** — jamais
`latest`, jamais un tag de branche qui flotte. Changer de version est une
décision qui se commite.

Les **LXC restent pour le système** : reverse proxy, MQTT, DNS. **L'existant ne
migre pas** — un conteneur qui marche n'est pas une raison de travailler.

Corollaire : un service applicatif emmène **sa** base dans **sa** pile. Une base
mutualisée n'a de sens qu'à partir du deuxième vrai locataire, et jusque-là elle
n'apporte qu'une reprise en deux temps.

### 6. La forme

- Bibliothèque **standard uniquement**. Aucun `pip install` nulle part.
- **Jamais de chaîne shell construite par concaténation.** Tout est un `argv`
  passé à `subprocess` : aucun shell n'intervient, donc rien n'est à échapper.
  Ni `shell=True`, ni commande assemblée à la main.
- `set -euo pipefail` en tête des scripts shell. Attention : `[[ … ]] && die`
  fait sortir en 1 dans le cas où le test est faux — utiliser `if`, ou `||`.
- **Chemins absolus partout** : le `PATH` de systemd est minimal.
- Journalisation horodatée et préfixée : `[STEP ]`, `[WARN ]`, `[ERROR]`.
- Paramétrage par variables d'environnement avec valeurs par défaut en tête de
  fichier ; les valeurs propres à la machine dans l'unité systemd ou un
  `/etc/default/<outil>` hors dépôt. **Aucun fichier de configuration à
  analyser** — si systemd sait le lire, le code n'a pas à le parser.
- **Commentaires en français, expliquant le _pourquoi_** et non le _quoi_.
- **Échouer bruyamment**, avec un message qui dit quoi faire et **un code de
  retour distinct par famille de panne**. Un échec silencieux est pire qu'une
  absence de sauvegarde.
- **Ne jamais armer un automatisme dont les prérequis manquent** : poser les
  fichiers, laisser le timer inactif, le dire.

## Prudence

- **Aucun secret dans le dépôt**, entrée `.gitignore` à l'appui. Un `.env` vit
  chiffré sur le poste et arrive par `scp` — **jamais par `git pull`** : la clé
  qui le déchiffre n'a rien à faire sur la machine qu'elle protège.
- Vers un stockage distant : **`copy`, jamais `sync`** — `sync` réplique les
  suppressions. Aucune opération d'écrasement ni de suppression distante. Le
  compte de service est délibérément incomplet (`objectViewer` +
  `objectCreator`), et **la rétention distante est une règle de cycle de vie du
  bucket**, jamais l'affaire du code.
- **Ne jamais proposer un test destructif** pour vérifier une protection : elle
  se lit dans le code et dans les droits, elle ne s'éprouve pas en effaçant.
- Une commande destructive **se tape à la main**, avec son avertissement, et
  reste hors des scripts. Un `mkfs` dans un script est un `mkfs` qu'on lancera
  un jour sans le relire.

## Documentation : un README court, un runbook détaillé

Jamais un seul gros fichier. Un README court à la racine du service, le reste
dans `doc/` :

| Fichier | Contenu | Cible |
|---|---|---|
| `README.md` | **ce qu'on tape** — fiche d'identité, gestes courants, une table « symptôme → où regarder », où va chaque fichier, le reste à faire | ~120 lignes, pas plus |
| `doc/RUNBOOK.md` | le détail — création, conception, pièges rencontrés en production, procédures rares | aussi long qu'il faut |
| `doc/PRA.md` | **une procédure de reprise par scénario**, du dégât local à la perte du nœud | une entrée par scénario |
| `doc/PRA-exercice.md` | comment jouer le PRA pour de faux, et mesurer le RTO | cases à cocher et champs à remplir |

- Sections **numérotées et stables** dans le runbook : les scripts y renvoient
  par numéro dans leurs messages d'erreur (« voir doc/RUNBOOK.md section 2 »),
  et un `Documentation=` d'unité systemd pointe dessus. Déplacer un document,
  c'est aussi corriger ces renvois-là.
- Sommaire cliquable en tête du runbook, liens ancrés depuis le README.
- Un piège rencontré en production se documente **avec sa date** et le message
  d'erreur exact — c'est ce qui le rend reconnaissable la fois suivante.
- Le README décrit ce que **fait le script**, pas une suite de commandes à
  retaper. Les commandes équivalentes vont dans le runbook, pour comprendre et
  rejouer à la main.

### Le PRA a ses propres règles

- **Un scénario, une procédure complète.** Le PRA se répète volontairement : en
  reprise on ne lit pas un document, on va à son cas et on doit y trouver tout
  ce qu'il faut sans naviguer. C'est le seul endroit où la duplication est un
  choix — y compris quand cela veut dire recopier une commande de création de
  machine que le runbook porte déjà.
- Il commence par une **table de diagnostic** — ce qu'on constate → le scénario
  — et non par une explication.
- Il annonce le **RPO** franchement, et laisse le **RTO vide tant qu'il n'a pas
  été mesuré** par un exercice. Une durée estimée de tête n'a aucune valeur le
  jour où on en a besoin.
- L'exercice s'écrit avec des **cases à cocher, des champs de mesure et un
  journal** à remplir. Il liste ses **garde-fous** en tête — ce qui, joué
  distraitement, casserait la production — et se termine par un démontage dont
  aucune étape n'est facultative.
- Il dit aussi **ce qu'il ne couvre pas**, pour qu'un exercice réussi ne se
  confonde pas avec « on est couvert ».

### Une circularité assumée s'écrit

Ce dépôt est servi par Forgejo, et il configure Forgejo. Une boucle de ce genre
est acceptable — mais **elle doit être écrite, avec ses sorties nommées**. Une
boucle assumée qui n'est écrite nulle part redevient une boucle subie.

## Vérifier sans accès à l'infrastructure

Le travail se fait dans le dépôt ; l'utilisateur installe et teste lui-même.
Donc : ne rien exécuter sur l'infrastructure, ne rien inventer sur son état,
**demander l'information qui manque** plutôt que de la deviner — une IP, un
VMID, un nom de bucket.

### Un défaut se reproduit AVANT d'être corrigé

Dès qu'un défaut est constaté — en production, ou en relisant — l'ordre est
**toujours** :

1. le reproduire, avec la **donnée exacte observée** : la ligne de journal, le
   code de retour, la sortie brute ;
2. **le voir échouer** ;
3. corriger ;
4. le revoir passer.

Jamais l'inverse. Une vérification écrite après le correctif n'a jamais échoué :
rien ne prouve qu'elle discrimine quoi que ce soit. **Voir le rouge est la seule
preuve qu'on parle du bon phénomène.** Le message de commit dit les deux : ce
qui a été constaté, et que le défaut a été vu d'abord.
