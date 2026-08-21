"""La documentation est un jeu de tests de la ligne de commande.

On extrait mécaniquement des documents les liens, les ancres, les chemins du
dépôt, les sous-commandes et les drapeaux, puis on les confronte au parseur et
à l'arbre RÉELS.

Ce contrôle n'est pas de la coquetterie : côté `pg`, c'est lui qui a trouvé que
`pg deploy --ctid 299` — la forme écrite partout, et celle dont l'exercice de
PRA dépend — n'existait pas. Une relecture à l'œil ne l'aurait pas vu, parce
qu'on lit ce qu'on croit avoir écrit.

Il couvre aussi les renvois PAR NUMÉRO. Les messages d'erreur du code et le
`Documentation=` des unités systemd pointent vers « doc/RUNBOOK.md section N » :
déplacer une section sans corriger ces renvois donne une erreur qui envoie
lire autre chose — au pire moment.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SERVICE = REPO / "pve-eranikus" / "forgejo"

DOCUMENTS = sorted(SERVICE.rglob("*.md"))
SOURCES = sorted((SERVICE / "fjtool").rglob("*.py"))
UNITES = sorted((SERVICE / "ct").glob("*.service")) + \
    sorted((SERVICE / "host").glob("*.service"))


def _relatif(chemin: Path) -> str:
    return str(chemin.relative_to(REPO))


def _ancre(titre: str) -> str:
    """L'ancre que produit un titre Markdown, à la façon de GitHub.

    Minuscules, ponctuation retirée, espaces en tirets. Les lettres accentuées
    sont conservées : ce sont des caractères alphanumériques Unicode, et les
    titres de ce dépôt en sont pleins.
    """
    titre = titre.strip().lstrip("#").strip()
    # Le contenu des liens et du code inline compte pour son texte.
    titre = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", titre)
    titre = titre.replace("`", "")
    sortie = []
    for caractere in titre.lower():
        if caractere.isalnum() or caractere in "-_":
            sortie.append(caractere)
        elif caractere.isspace():
            sortie.append("-")
        # tout le reste est retiré
    return "".join(sortie)


def _titres(chemin: Path) -> set[str]:
    ancres = set()
    dans_bloc = False
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        if ligne.lstrip().startswith("```"):
            dans_bloc = not dans_bloc
            continue
        if dans_bloc or not ligne.startswith("#"):
            continue
        ancres.add(_ancre(ligne))
    return ancres


# Un lien Markdown relatif : [texte](chemin) ou [texte](chemin#ancre).
LIEN = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def test_il_y_a_bien_des_documents_a_verifier():
    assert DOCUMENTS, "aucun document trouvé — le test ne vérifie rien"
    assert SOURCES, "aucune source trouvée — le test ne vérifie rien"


# ─── les liens ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("document", DOCUMENTS, ids=_relatif)
def test_les_liens_relatifs_pointent_sur_un_fichier_existant(document):
    """Un lien mort dans un PRA se découvre le jour où on en a besoin."""
    manquants = []
    for cible in LIEN.findall(document.read_text(encoding="utf-8")):
        if cible.startswith(("http://", "https://", "mailto:", "#")):
            continue
        chemin, _, _ = cible.partition("#")
        if not chemin:
            continue
        if not (document.parent / chemin).resolve().exists():
            manquants.append(cible)
    assert not manquants, f"{document.name} : liens morts {manquants}"


@pytest.mark.parametrize("document", DOCUMENTS, ids=_relatif)
def test_les_ancres_existent_dans_le_document_vise(document):
    """Une ancre fausse renvoie en haut de page, silencieusement — et le
    lecteur croit avoir lu la bonne section."""
    manquantes = []
    for cible in LIEN.findall(document.read_text(encoding="utf-8")):
        if cible.startswith(("http://", "https://", "mailto:")):
            continue
        chemin, sep, ancre = cible.partition("#")
        if not sep or not ancre:
            continue
        vise = document if not chemin else (document.parent / chemin).resolve()
        if not vise.exists() or vise.suffix != ".md":
            continue
        if ancre not in _titres(vise):
            manquantes.append(cible)
    assert not manquantes, f"{document.name} : ancres absentes {manquantes}"


# ─── les chemins du dépôt cités dans les documents ───────────────────────────

# Un chemin du service cité en `code inline` : ct/…, host/…, fjtool/…, doc/…
CHEMIN = re.compile(r"`((?:ct|host|fjtool|doc)/[A-Za-z0-9_./-]+)`")


# Les fichiers que la documentation nomme et qui sont DÉLIBÉRÉMENT absents du
# dépôt. Chaque entrée doit porter sa raison : sans cette liste, on finirait
# par committer un fichier bidon pour faire taire le test.
#
# Vide aujourd'hui, et c'est une bonne nouvelle : `ct/RELEASE-KEY.asc` y a
# figuré tant que la clé de publication devait être déposée à la main. Elle est
# maintenant récupérée par `fj key --fetch` et commitée comme le reste, donc
# l'exception n'a plus lieu d'être — c'est le test ci-dessous qui l'a signalé.
ABSENTS_DELIBERES: set[str] = set()


@pytest.mark.parametrize("document", DOCUMENTS, ids=_relatif)
def test_les_chemins_cites_existent(document):
    """Un document qui nomme `ct/pg_hba.conf` alors que le fichier s'appelle
    autrement envoie chercher un fichier qui n'existe pas."""
    manquants = [
        chemin
        for chemin in CHEMIN.findall(document.read_text(encoding="utf-8"))
        if chemin not in ABSENTS_DELIBERES
        if not (SERVICE / chemin).exists()
    ]
    assert not manquants, f"{document.name} : chemins inexistants {manquants}"


def test_les_absences_deliberees_le_sont_toujours():
    """Le jour où l'un de ces fichiers est réellement déposé, il doit sortir
    de la liste — sinon elle deviendrait une liste d'exceptions périmées, et
    plus personne ne saurait laquelle protège encore quelque chose."""
    presents = [c for c in ABSENTS_DELIBERES if (SERVICE / c).exists()]
    assert not presents, (
        f"{presents} existe(nt) maintenant : retirer de ABSENTS_DELIBERES"
    )


# ─── les sous-commandes et les drapeaux ──────────────────────────────────────

# On extrait les invocations des BLOCS DE CODE et des SPANS `…`, jamais de la
# prose. Une phrase comme « `fj version --resolve` interroge Codeberg » ferait
# sinon croire à un argument « interroge » : le contrôle rougirait sur une
# documentation parfaitement juste, et on finirait par le désarmer.
SPAN = re.compile(r"`([^`\n]+)`")


def _invocations(texte: str) -> list[list[str]]:
    """Les lignes de commande `fj …` d'un document, découpées en argv."""
    candidates: list[str] = []

    dans_bloc = False
    for ligne in texte.splitlines():
        if ligne.lstrip().startswith("```"):
            dans_bloc = not dans_bloc
            continue
        if dans_bloc:
            candidates.append(ligne)
        else:
            candidates.extend(SPAN.findall(ligne))

    invocations = []
    for brut in candidates:
        # Un commentaire de fin de ligne n'est pas un argument.
        brut = brut.split("#", 1)[0]
        morceaux = brut.split()
        if not morceaux:
            continue
        # Le lanceur peut être nommé par son chemin dans le dépôt.
        premier = morceaux[0]
        if premier != "fj" and not premier.endswith("/fj"):
            continue
        # `fj` seul, cité comme un nom dans une phrase ou un tableau, n'est
        # pas une invocation : l'analyser produirait un « sous-commande
        # requise » qui ne dit rien sur la documentation.
        if len(morceaux) == 1:
            continue
        invocations.append(morceaux[1:])
    return invocations


def _sous_commandes() -> set[str]:
    from fjtool.cli import construire_parseur

    parseur = construire_parseur()
    for action in parseur._actions:
        if action.dest == "commande" and action.choices:
            return set(action.choices)
    raise AssertionError("aucun sous-parseur trouvé")


def test_les_documents_citent_bien_des_invocations():
    """Un extracteur qui ne trouve rien passerait pour un contrôle vert."""
    total = sum(
        len(_invocations(d.read_text(encoding="utf-8"))) for d in DOCUMENTS
    )
    assert total >= 15, f"seulement {total} invocations extraites"


@pytest.mark.parametrize("document", DOCUMENTS, ids=_relatif)
def test_les_sous_commandes_citees_existent(document):
    """Documenter une commande qui n'existe pas est le défaut que ce contrôle
    a trouvé côté `pg`."""
    connues = _sous_commandes()
    inventees = set()
    for argv in _invocations(document.read_text(encoding="utf-8")):
        verbes = [m for m in argv if not m.startswith("-")]
        # Le premier mot qui n'est pas une option, et qui ne suit pas une
        # option à valeur, est la sous-commande.
        for morceau in argv:
            if morceau.startswith("-"):
                continue
            if morceau in connues:
                break
            if verbes and morceau == verbes[0]:
                inventees.add(morceau)
            break
    assert not inventees, (
        f"{document.name} : sous-commandes inexistantes {sorted(inventees)} "
        f"(connues : {sorted(connues)})"
    )


# Les valeurs de remplacement des documents, qui ne sont pas des arguments
# réels : « <ID> », « <NOM> », « … ».
GABARIT = re.compile(r"^[<{]|[>}]$|^\.\.\.$|^…$")


@pytest.mark.parametrize("document", DOCUMENTS, ids=_relatif)
def test_les_drapeaux_cites_sont_acceptes_par_le_parseur(document):
    """On ANALYSE réellement chaque invocation trouvée dans les documents.

    C'est le seul contrôle qui prouve que la forme écrite est la forme
    acceptée — et non une forme voisine qui a l'air juste. Côté `pg`, c'est
    lui qui a trouvé que `pg deploy --ctid 299` n'existait pas.
    """
    from fjtool.cli import construire_parseur

    refusees = []
    for argv in _invocations(document.read_text(encoding="utf-8")):
        if any(GABARIT.search(m) for m in argv):
            continue  # exemple à trous, rien à analyser
        try:
            construire_parseur().parse_args(argv)
        except SystemExit:
            refusees.append("fj " + " ".join(argv))
    assert not refusees, f"{document.name} : invocations refusées {refusees}"


# ─── les renvois par numéro de section ───────────────────────────────────────

RENVOI = re.compile(r"RUNBOOK\.md\s+section\s+(\d+)", re.IGNORECASE)
RENVOI_ANCRE = re.compile(r"RUNBOOK\.md#(\d+)-")


def _sections_du_runbook() -> set[str]:
    runbook = SERVICE / "doc" / "RUNBOOK.md"
    return {
        m.group(1)
        for ligne in runbook.read_text(encoding="utf-8").splitlines()
        if ligne.startswith("## ")
        for m in [re.match(r"## (\d+)\.", ligne)]
        if m
    }


@pytest.mark.parametrize("source", SOURCES, ids=_relatif)
def test_les_renvois_du_code_visent_une_section_reelle(source):
    """Un message d'erreur qui dit « voir section 4 » alors que la section 4
    parle d'autre chose envoie lire au mauvais endroit — au pire moment."""
    sections = _sections_du_runbook()
    texte = source.read_text(encoding="utf-8")
    faux = [n for n in RENVOI.findall(texte) if n not in sections]
    faux += [n for n in RENVOI_ANCRE.findall(texte) if n not in sections]
    assert not faux, (
        f"{source.name} : renvoie vers des sections inexistantes {faux} "
        f"(existantes : {sorted(sections, key=int)})"
    )


@pytest.mark.parametrize("document", DOCUMENTS, ids=_relatif)
def test_les_renvois_des_documents_visent_une_section_reelle(document):
    sections = _sections_du_runbook()
    texte = document.read_text(encoding="utf-8")
    faux = [n for n in RENVOI.findall(texte) if n not in sections]
    faux += [n for n in RENVOI_ANCRE.findall(texte) if n not in sections]
    assert not faux, f"{document.name} : sections inexistantes {faux}"


# ─── le Documentation= des unités systemd ────────────────────────────────────


@pytest.mark.parametrize("unite", UNITES, ids=_relatif)
def test_le_documentation_des_unites_pointe_sur_un_fichier_du_depot(unite):
    """`systemctl status` affiche cette ligne. Elle doit mener quelque part.

    Le chemin est celui du NŒUD (`/root/homelab_proxmox/...`) : on vérifie donc
    qu'il correspond à un fichier réel de ce dépôt, une fois le préfixe retiré.
    """
    for ligne in unite.read_text(encoding="utf-8").splitlines():
        if not ligne.startswith("Documentation="):
            continue
        cible = ligne.partition("=")[2].strip()
        assert cible.startswith("file:///root/homelab_proxmox/"), cible
        relatif = cible[len("file:///root/homelab_proxmox/"):]
        assert (REPO / relatif).is_file(), f"{unite.name} → {relatif} absent"


# ─── le sommaire du runbook ──────────────────────────────────────────────────


def test_le_sommaire_du_runbook_couvre_toutes_ses_sections():
    """Un sommaire incomplet est pire qu'absent : il fait croire qu'une
    section n'existe pas."""
    runbook = SERVICE / "doc" / "RUNBOOK.md"
    texte = runbook.read_text(encoding="utf-8")
    titres = [
        ligne for ligne in texte.splitlines()
        if re.match(r"## \d+\.", ligne)
    ]
    ancres_du_sommaire = {
        cible.lstrip("#")
        for cible in LIEN.findall(texte)
        if cible.startswith("#")
    }
    absents = [t for t in titres if _ancre(t) not in ancres_du_sommaire]
    assert not absents, f"sections absentes du sommaire : {absents}"
