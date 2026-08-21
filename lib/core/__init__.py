"""Briques génériques, sans aucune connaissance de Proxmox ni d'un service.

Ce paquet est le SEUL qui soit poussé dans les conteneurs. Deux règles en
découlent, et elles se vérifient mécaniquement (voir tests/) :

  - il n'importe jamais `proxmox` — un conteneur n'a rien à faire avec `pct`,
    et le paquet n'y est même pas déposé ;
  - il ne nomme aucun service. Si « postgres » apparaît ici, le code est au
    mauvais endroit.

Bibliothèque standard uniquement : rien à installer sur l'hyperviseur ni dans
un conteneur.
"""

from __future__ import annotations

import sys

MIN_PYTHON = (3, 11)


def require_python(minimum: tuple[int, int] = MIN_PYTHON) -> None:
    """Refuse de démarrer sous une version trop ancienne.

    `python3` vient du template Debian, pas d'une décision explicite : rien ne
    garantit sa version sur un conteneur recréé autrement. Mieux vaut un refus
    net en tête d'exécutable qu'une `SyntaxError` au milieu d'une restauration.

    À appeler depuis le point d'entrée AVANT d'importer quoi que ce soit qui
    dépende d'une syntaxe récente.

    LE MESSAGE NE DÉSIGNE AUCUNE MACHINE, et c'est le fruit d'un défaut réel.
    Il disait « installer python3 dans le conteneur » ; joué depuis le dépôt
    sur un poste macOS, il donnait :

        python3 3.11 minimum requis, 3.9.6 trouvé
        (/Library/Developer/CommandLineTools/usr/bin/python3).
        Installer python3 dans le conteneur.

    Aucun conteneur dans l'histoire, et un python 3.14 parfaitement utilisable
    à côté. Le message envoyait corriger la bonne chose au mauvais endroit — le
    pire genre de message d'erreur, parce qu'on le suit. `core` est le paquet
    générique : il ne sait pas s'il tourne sur un nœud, dans un conteneur ou
    sur un poste, et il n'a donc rien à en dire.

    La cause n'est presque jamais « python est trop vieux » mais le SHEBANG :
    `#!/usr/bin/python3` est un chemin absolu, choisi parce que le PATH de
    systemd et de `pct exec` est minimal. Sur un poste où `/usr/bin/python3`
    est plus ancien que le reste du système, c'est cet interpréteur-là qui est
    pris — pas celui du PATH. Le message le dit maintenant, et donne les deux
    issues : contourner pour cette fois, ou corriger la machine.
    """
    if sys.version_info < minimum:
        found = ".".join(str(n) for n in sys.version_info[:3])
        wanted = ".".join(str(n) for n in minimum)
        # `sys.argv[0]` plutôt qu'un nom en dur : ce module est partagé par
        # plusieurs exécutables, et nommer l'un d'eux ici les trahirait tous
        # sauf un.
        executable = (sys.argv[0] if sys.argv else "") or "<exécutable>"
        raise SystemExit(
            f"python3 {wanted} minimum requis, {found} trouvé "
            f"({sys.executable}).\n"
            "         Cet exécutable porte « #!/usr/bin/python3 » en tête : un "
            "chemin ABSOLU,\n"
            "         parce que le PATH de systemd et de « pct exec » est "
            "minimal. C'est donc\n"
            "         CET interpréteur-là qui est trop ancien, pas celui du "
            "PATH.\n"
            f"         Contourner : python3.13 {executable} …\n"
            "         Corriger    : fournir un python3 récent en "
            "/usr/bin/python3."
        )
