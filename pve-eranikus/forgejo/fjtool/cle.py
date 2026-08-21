"""La clé de publication Forgejo : la récupérer une fois, l'épingler ensuite.

CE QUE CE MODULE REMPLACE. La première version exigeait qu'on dépose
`ct/RELEASE-KEY.asc` à la main « après l'avoir confrontée à une source
indépendante ». C'était juste sur le papier et inutilisable en pratique :
personne ne mène une enquête de provenance pour un homelab, donc la clé serait
venue du site du projet de toute façon — et la cérémonie n'aurait rien acheté.

CE QU'ON GARDE, ET QUI VAUT VRAIMENT QUELQUE CHOSE : **l'épinglage**. La clé
est récupérée une fois, son empreinte est écrite dans le dépôt et commitée. À
partir de là, chaque déploiement vérifie que la clé qui sert à valider le
binaire est TOUJOURS celle-là. Un changement de clé de signature — qu'il vienne
du projet ou d'ailleurs — devient un refus, et un `git diff` lisible.

C'est exactement la logique appliquée à la version : une valeur décidée une
fois, tracée dans git, et relue à chaque passage. La différence entre « je fais
confiance à ce que le serveur me donne aujourd'hui » et « je fais confiance à
ce que j'ai approuvé, et je le vérifie ».

CE QUE ÇA NE PROTÈGE PAS. Si la toute première récupération est compromise, on
épingle la mauvaise clé et on la vérifie fidèlement ensuite. C'est le défaut
connu de la confiance à la première utilisation, et il est assumé : la
protection porte sur les mises à jour, pas sur l'amorçage. Pour durcir
l'amorçage, comparer l'empreinte affichée à celle annoncée ailleurs que sur la
page de téléchargement — c'est l'affaire d'une minute, et c'est facultatif.
"""

from __future__ import annotations

import re
import urllib.request
from pathlib import Path

from core.runner import ligne_utile

# Le Web Key Directory de `contact@forgejo.org`, tel que la page de
# téléchargement officielle le désigne. Ce n'est pas une URL devinée : elle a
# été relevée sur https://forgejo.org/download/ le 21 août 2026, et l'adresse
# elle-même est DÉRIVÉE de l'adresse de courriel — le « hu/… » est un hachage
# de la partie locale. Elle est donc stable tant que l'adresse l'est.
#
# CE QUE CE CHOIX APPORTE, et c'est le point : le WKD vit sur
# `openpgpkey.forgejo.org`, un domaine DIFFÉRENT de `codeberg.org` d'où vient
# le binaire. La clé et l'artefact ne voyagent donc pas par le même canal, ce
# qui est exactement la propriété qu'une vérification de signature exige pour
# valoir mieux qu'une somme de contrôle.
#
# Une première version de ce fichier portait « https://forgejo.org/forgejo.gpg »,
# devinée faute d'accès réseau. Elle rendait 404 — pire que rien : ça ressemble
# à un défaut du code plutôt qu'à un geste manquant.
URL_PAR_DEFAUT = (
    "https://openpgpkey.forgejo.org/.well-known/openpgpkey/forgejo.org/hu/"
    "dj3498u4hyyarh35rkjfnghbjxug6b19"
)

TIMEOUT = 30

# Une empreinte GPG v4 : 40 caractères hexadécimaux. `gpg` l'affiche souvent
# par groupes de quatre ; on range avant de comparer, sinon deux écritures de
# la même clé se liraient comme deux clés.
_EMPREINTE = re.compile(r"^[0-9A-F]{40}$")


class CleError(RuntimeError):
    """Clé inutilisable. Toujours dit avec ce qu'il faut taper ensuite."""


def normaliser(brut: str) -> str:
    """« ABCD 1234 … » → « ABCD1234… ». Espaces retirés, majuscules.

    Le format à espaces est celui que `gpg --fingerprint` montre à l'écran, et
    c'est donc celui qu'un humain recopie. Le refuser obligerait à retaper.
    """
    return re.sub(r"\s+", "", brut).upper()


def valider(empreinte: str) -> str:
    range_ = normaliser(empreinte)
    if not _EMPREINTE.fullmatch(range_):
        raise CleError(
            f"empreinte mal formée : « {empreinte} » — "
            "attendu 40 caractères hexadécimaux"
        )
    return range_


def parse(texte: str) -> str | None:
    """La première ligne utile d'un fichier d'empreinte, ou None.

    `None` veut dire « non épinglée », et jamais « n'importe laquelle ».
    """
    for ligne in texte.splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#"):
            continue
        return ligne
    return None


def lire(chemin: Path) -> str | None:
    if not chemin.is_file():
        return None
    brut = parse(chemin.read_text(encoding="utf-8"))
    return normaliser(brut) if brut else None


def rendre(empreinte: str, *, source: str) -> str:
    """Le contenu du fichier d'empreinte, en-tête compris.

    La SOURCE est consignée avec l'empreinte : six mois plus tard, « d'où vient
    cette clé » est la première question, et un fichier qui ne porte que
    quarante caractères hexadécimaux n'y répond pas.
    """
    return (
        "# Empreinte de la clé qui signe les publications Forgejo.\n"
        "#\n"
        "# Une seule ligne utile : 40 caractères hexadécimaux. Tout ce qui\n"
        "# commence par « # » est un commentaire.\n"
        "#\n"
        "# ÉPINGLÉE. « fj deploy » refuse d'installer si la clé de\n"
        "# ct/RELEASE-KEY.asc ne correspond plus à cette empreinte. Un\n"
        "# changement de clé de signature devient donc un refus, et non une\n"
        "# chose qu'on avale sans la voir.\n"
        "#\n"
        f"# Récupérée depuis : {source}\n"
        "#\n"
        "# La changer est une DÉCISION : vérifier la nouvelle empreinte auprès\n"
        "# du projet, puis rejouer « fj key --fetch » et commiter le diff.\n"
        "\n"
        f"{empreinte}\n"
    )


# ─── récupération ────────────────────────────────────────────────────────────


def recuperer(source: str, *, timeout: int = TIMEOUT) -> bytes:
    """Le bloc de clé, depuis une URL **ou un fichier local**.

    Accepter un chemin local n'est pas une commodité : c'est ce qui permet de
    récupérer la clé par le moyen qu'on veut — une autre machine, une clé USB,
    un gestionnaire de mots de passe — sans que ce code n'ait à connaître ce
    moyen.
    """
    chemin = Path(source)
    if chemin.is_file():
        return chemin.read_bytes()
    if not source.startswith(("http://", "https://")):
        raise CleError(
            f"« {source} » n'est ni un fichier existant ni une URL http(s)"
        )
    try:
        with urllib.request.urlopen(source, timeout=timeout) as reponse:
            return reponse.read()
    except Exception as exc:  # noqa: BLE001 - toute panne réseau, même cause
        raise CleError(
            f"clé injoignable ({type(exc).__name__}: {exc})\n"
            f"         source essayée : {source}\n"
            "         récupérer le bloc de clé à la main, puis :\n"
            "         fj key --fetch --from <fichier>"
        ) from exc


def empreintes(runner, bloc: Path) -> list[str]:
    """Les empreintes contenues dans un bloc de clé, SANS l'importer.

    `--show-keys` lit le fichier et décrit ce qu'il contient sans rien ajouter
    à un trousseau. C'est ce qui permet de REGARDER une clé avant de décider
    de s'en servir — l'inverse d'un `--import` qui engage avant de montrer.
    """
    res = runner.read(
        "gpg", "--show-keys", "--with-colons", str(bloc), check=False
    )
    if not res.ok:
        raise CleError(
            "gpg n'a pas su lire ce bloc de clé — est-ce bien une clé "
            "publique au format ASCII ou binaire ?\n"
            + ligne_utile(res.stderr)
        )
    trouvees = [
        ligne.split(":")[9]
        for ligne in res.lines
        if ligne.startswith("fpr:")
    ]
    if not trouvees:
        raise CleError("aucune empreinte dans ce bloc de clé")
    return trouvees


def retenir(trouvees: list[str], *, epinglee: str | None) -> str:
    """L'empreinte à retenir parmi celles du bloc.

    Un bloc peut porter plusieurs clés — une principale et ses sous-clés, ou
    plusieurs clés de signature en cours de rotation. Deux cas, et ils ne se
    confondent pas :

      - rien n'est encore épinglé : on retient la PREMIÈRE, qui est la clé
        principale dans l'ordre où gpg les rend ;
      - quelque chose est épinglé : on retient celle qui correspond, et
        l'absence est une erreur. C'est tout l'intérêt de l'épinglage — le
        bloc a changé, on veut le savoir plutôt que de suivre.
    """
    normalisees = [normaliser(f) for f in trouvees]
    if epinglee is None:
        return normalisees[0]
    if epinglee in normalisees:
        return epinglee
    raise CleError(
        "la clé récupérée ne correspond PAS à l'empreinte épinglée.\n"
        f"         épinglée : {epinglee}\n"
        "         trouvée(s) : " + ", ".join(normalisees) + "\n"
        "         Rien n'est installé. Soit le projet a changé de clé de\n"
        "         signature — le vérifier auprès de lui, puis rejouer\n"
        "         « fj key --fetch » et commiter le diff —, soit la source\n"
        "         n'est pas celle qu'on croit."
    )
