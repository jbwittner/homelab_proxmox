"""Les sections de `pg deploy`, une par module.

Le découpage reprend celui du déployeur bash — A prérequis conteneur, B pose
dans le CT, C contrôles, D outillage de l'hôte, E paquets, F hors-site, G
première sauvegarde et secrets. Il l'a été pour que les deux `--status`
restent comparables pendant la migration ; la comparaison faite, il reste
parce qu'il est bon.

S'y ajoute **H, les retraits** : ce que plus rien n'appelle. Une bascule
franche sans étape de retrait laisse sur le nœud des exécutables périmés que
quelqu'un rejouera.

Tout ce paquet est du code d'HÔTE : il lui faut `pct`. Le conteneur ne
l'importe jamais, et `cli` s'en assure par ses imports paresseux.
"""
