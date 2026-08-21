"""Le refus de démarrer sous un interpréteur trop ancien.

C'est le tout premier message que produisent `pg` et `fj`, avant même que le
reste ne s'analyse. Il doit donc être juste **quelle que soit la machine** —
et c'est exactement ce qu'il n'était pas.

DÉFAUT CONSTATÉ LE 21 AOÛT 2026, sur un poste de développement (macOS), en
jouant `./pve-eranikus/forgejo/fj version --resolve` depuis le dépôt :

    python3 3.11 minimum requis, 3.9.6 trouvé
    (/Library/Developer/CommandLineTools/usr/bin/python3).
    Installer python3 dans le conteneur.

Le chemin affiché est celui d'un Mac. Il n'y a aucun conteneur dans l'histoire,
et l'utilisateur avait par ailleurs un python 3.14 parfaitement utilisable. Le
message envoyait donc corriger la bonne chose au mauvais endroit — le pire
genre de message d'erreur, parce qu'on le suit.

La cause n'est pas la version installée mais le **shebang** : `#!/usr/bin/python3`
est un chemin ABSOLU, choisi parce que le PATH de systemd et de `pct exec` est
minimal. Sur un poste où `/usr/bin/python3` est plus ancien que le reste du
système, c'est cet interpréteur-là qui est pris, et pas celui du PATH.
"""

from __future__ import annotations

import pytest

from core import MIN_PYTHON, require_python


def _refus() -> str:
    """Le message rendu par un refus. Un minimum inatteignable le déclenche
    à coup sûr, sans dépendre de la version qui joue les tests."""
    with pytest.raises(SystemExit) as sortie:
        require_python((99, 0))
    return str(sortie.value)


def test_un_interpreteur_conforme_ne_leve_pas():
    """La suite tourne forcément sous un interpréteur conforme : si ce test
    échoue, c'est le seuil lui-même qui est faux."""
    require_python(MIN_PYTHON)


def test_le_refus_nomme_l_interpreteur_reellement_pris():
    """C'est la seule information qui a permis de diagnostiquer le cas réel :
    sans le chemin, on cherche du côté du `python` du PATH, qui va bien."""
    import sys

    assert sys.executable in _refus()


def test_le_refus_ne_presume_pas_qu_on_est_dans_un_conteneur():
    """« Installer python3 dans le conteneur » sur un poste macOS envoie
    corriger la bonne chose au mauvais endroit.

    `core` est le paquet générique : il ne sait pas où il tourne — nœud,
    conteneur ou poste de développement — et son message ne doit donc en
    désigner aucun.
    """
    message = _refus()
    assert "conteneur" not in message.lower(), (
        "le refus ne doit désigner aucune machine en particulier"
    )


def test_le_refus_explique_que_la_cause_est_le_shebang():
    """Sans cette phrase, l'utilisateur conclut que son python est trop vieux
    — alors qu'il en a un bon, simplement pas à ce chemin-là.

    On cherche « #!/usr/bin/python3 » avec son « #! », et non le chemin nu :
    sur la machine qui joue les tests, `sys.executable` VAUT souvent
    `/usr/bin/python3`, et le chemin nu ferait donc passer ce test pour une
    raison qui n'a rien à voir avec ce qu'il vérifie.
    """
    assert "#!/usr/bin/python3" in _refus(), "le refus doit nommer le shebang"


def test_le_refus_dit_comment_sen_sortir_tout_de_suite():
    """Un refus qui ne dit pas quoi taper oblige à aller lire le runbook, et
    on ne va pas lire un runbook pour lancer une commande de lecture."""
    message = _refus()
    assert "python3.13" in message or "python3.X" in message, (
        "le refus doit montrer comment passer un autre interpréteur"
    )


def test_le_refus_donne_les_deux_versions():
    """Celle qu'on exige et celle qu'on a trouvée : sans les deux, on ne sait
    pas de combien on est loin."""
    message = _refus()
    assert "99.0" in message, "la version exigée doit apparaître"
    import sys

    trouvee = ".".join(str(n) for n in sys.version_info[:3])
    assert trouvee in message, "la version trouvée doit apparaître"
