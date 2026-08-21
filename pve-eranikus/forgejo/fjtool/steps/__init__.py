"""Les sections de `fj deploy`, une par module.

Le découpage reprend celui du CT PostgreSQL — A prérequis du conteneur, B pose
dans le CT, C contrôles, D outillage de l'hôte, F hors-site, G secrets et
première sauvegarde, H retraits — et s'y ajoute **V, l'installation binaire
épinglée**, qui n'a pas d'équivalent là-bas : PostgreSQL vient d'un dépôt de
paquets, Forgejo est un binaire téléchargé, vérifié et posé à la main.

Reprendre le découpage n'est pas de la coquetterie : deux services du même
dépôt qui se déploient de deux façons différentes coûtent deux
apprentissages, et c'est le second qu'on ne fait jamais.

Tout ce paquet est du code d'HÔTE : il lui faut `pct`. Le conteneur ne
l'importe jamais, et `cli` s'en assure par ses imports paresseux.
"""
