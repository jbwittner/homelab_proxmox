"""Les sections de `pg deploy`, une par module.

Le découpage suit celui de `pg-deploy.sh` — A prérequis conteneur, B pose dans
le CT, C contrôles, D outillage de l'hôte, E paquets, F hors-site, G première
sauvegarde et secrets — parce que les sorties `--status` des deux
implémentations doivent rester comparables pendant la migration.

Tout ce paquet est du code d'HÔTE : il lui faut `pct`. Le conteneur ne
l'importe jamais, et `cli` s'en assure par ses imports paresseux.
"""
