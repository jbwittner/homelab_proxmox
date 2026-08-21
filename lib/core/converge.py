"""Moteur de convergence — la boucle que le bash réécrivait quarante fois.

`pg-deploy.sh` répète, section après section, le triplet « constater / afficher
en dry-run / appliquer ». Quarante-quatre fois, sans garantie qu'un cas de
simulation n'ait été oublié — et sept de ses mutations ne passent d'ailleurs
pas par le garde `run()`, ce qui rend le mode simulation moins sûr qu'il n'en a
l'air. Ce module remplace cette répétition par une boucle unique.

L'IDÉE QUI SUPPRIME LA DUPLICATION : **le plan est produit par `check()`,
jamais par `apply()`.** Une étape constate, et renvoie la liste des actions
qu'il faudrait jouer — chacune portant son propre libellé et son propre
appelable. `apply()` devient générique : parcourir cette liste. Il n'existe
donc qu'UNE description du delta, et « zéro modification sur un état conforme »
n'est plus une discipline mais `actions == ()`.

LES TROIS MODES SONT UN SEUL PARCOURS :

    --status    check()          → un verdict par étape
    --dry-run   check()          → les mêmes verdicts, plus les libellés
    défaut      check() + apply() → entrelacés

L'ENTRELACEMENT PORTE TOUT. En mode APPLY on constate, on applique, puis on
constate l'étape suivante — jamais « tout constater puis tout appliquer », qui
ferait observer à la section B un conteneur d'avant son redémarrage.

CE QU'ON NE PEUT PAS SAVOIR SE DIT. En simulation, rien n'ayant été appliqué,
une étape dont un prérequis reste à poser ne peut rien conclure : elle est
`unknown`, pas `ok` et pas `error`. Le bash faisait ça au cas par cas (« pose
non evaluable ») ; c'est une règle du parcours ici.

Rien dans ce fichier ne connaît Proxmox ni PostgreSQL : le prochain service
réutilise la même boucle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Protocol, Sequence

from .log import info, step as log_step, warn


class Mode(Enum):
    APPLY = "apply"
    DRY_RUN = "dry-run"
    STATUS = "status"

    @property
    def applies(self) -> bool:
        return self is Mode.APPLY


# Verdicts qu'une étape peut rendre elle-même.
ETATS_CHECK = ("ok", "drift", "absent", "error")

# Verdicts que seul le parcours attribue.
SKIP = "skip"
UNKNOWN = "unknown"
BLOCKED = "blocked"


@dataclass(frozen=True)
class Action:
    """Une modification, avec de quoi l'annoncer sans la faire.

    Le libellé n'est pas une commodité d'affichage : c'est LA description du
    delta, et il n'y en a pas d'autre. Une phrase pour la simulation et un code
    pour l'exécution finiraient par diverger.
    """

    label: str
    run: Callable[["Context"], None]
    # Ce que cette action rend nécessaire ensuite : « ct.reboot »,
    # « ct.daemon-reload »… Déclarer plutôt que lever un drapeau à la main
    # permet au parcours de coalescer — trois copies d'unité ne provoquent
    # qu'un seul rechargement.
    effects: frozenset[str] = frozenset()
    # Une action qui fait apparaître un mot de passe ne se joue jamais par
    # défaut : rejouer un déploiement de routine ne doit pas en créer un dont
    # personne n'attend la rotation.
    generates_secret: bool = False


@dataclass(frozen=True)
class Outcome:
    """Ce qu'une étape a constaté, et ce qu'il faudrait faire.

    `state` répond « où en est-on », `actions` répond « que faire » — et c'est
    la seule fois où la seconde question est traitée.
    """

    state: str
    detail: str = ""
    actions: tuple[Action, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in ETATS_CHECK:
            raise ValueError(
                f"état inattendu : {self.state} (attendu {ETATS_CHECK})"
            )


@dataclass
class Report:
    """Une ligne du bilan."""

    step: str
    section: str
    state: str
    detail: str = ""
    applied: tuple[str, ...] = ()


class Step(Protocol):
    """Ce qu'une étape doit savoir faire.

    `check()` est sans effet de bord SUR LE SYSTÈME. Il lit, il compare, il
    décrit. C'est aussi pourquoi le Runner est en simulation dans les modes qui
    n'appliquent pas : une écriture égarée dans un `check()` mal écrit y est
    neutralisée plutôt qu'exécutée.
    """

    name: str
    section: str
    requires: tuple[str, ...]

    def skip_if(self, ctx: "Context") -> str | None: ...

    def check(self, ctx: "Context") -> Outcome: ...


@dataclass
class Context:
    """Ce que les étapes partagent, et ce qu'elles se transmettent.

    Les faits inter-étapes vivent dans `facts`, où **`None` signifie « non
    déterminé »** et jamais « faux ». C'est la version typée du `MP2_STATE=inconnu`
    du bash — et ça bouche un vrai trou : avec `--no-container`, la section A
    était sautée, `MP2_STATE` restait « inconnu », la garde `== divergent` ne se
    déclenchait pas, et le timer hors-site pouvait être armé sans que le volume
    ait jamais été vérifié.
    """

    mode: Mode = Mode.APPLY
    allow_secrets: bool = False
    facts: dict[str, object] = field(default_factory=dict)
    _handlers: dict[str, Callable[["Context"], None]] = field(default_factory=dict)
    _pending: dict[str, None] = field(default_factory=dict)

    def on_effect(self, nom: str, handler: Callable[["Context"], None]) -> None:
        """Déclare quoi faire quand un effet a été demandé."""
        self._handlers[nom] = handler

    def request(self, effets: Iterable[str]) -> None:
        # Un dictionnaire plutôt qu'un ensemble : l'ordre d'insertion est
        # conservé, donc l'ordre de vidange est celui de la demande.
        for effet in effets:
            self._pending[effet] = None

    def pending(self) -> tuple[str, ...]:
        """Les effets demandés et pas encore vidés, dans l'ordre de demande."""
        return tuple(self._pending)

    def flush(self) -> list[str]:
        """Vide les effets demandés, chacun une fois, dans l'ordre.

        En simulation, ils sont ANNONCÉS et non joués. C'est ce qui permet à
        `--dry-run` de dire « il faudra redémarrer le CT » — l'information la
        plus utile de ce mode, et celle qu'un vidage réservé à l'exécution
        ferait disparaître exactement là où on la cherche.
        """
        joues: list[str] = []
        while self._pending:
            nom = next(iter(self._pending))
            del self._pending[nom]
            if nom not in self._handlers:
                continue
            if self.mode.applies:
                self._handlers[nom](self)
            elif self.mode is Mode.DRY_RUN:
                info(f"  [dry-run] effet : {nom}")
            # En STATUS, on draine sans rien dire : ce mode rend des verdicts,
            # pas un plan. Y annoncer un redémarrage ferait croire qu'il décrit
            # ce qu'il ferait, ce qui est le rôle de --dry-run.
            joues.append(nom)
        return joues


def traverse(
    steps: Sequence[Step],
    ctx: Context,
    *,
    effets_finaux: Sequence[str] = (),
) -> list[Report]:
    """Le parcours unique. Renvoie une ligne de bilan par étape.

    Les effets demandés sont vidés APRÈS la dernière étape : c'est ainsi que
    « redémarrer après tous les `pct set` » s'exprime — en effet coalescé, pas
    en ordre de déclaration.

    `effets_finaux` sont demandés systématiquement, qu'une action les ait
    déclarés ou non. Un seul cas les justifie, et il est réel : les fichiers de
    configuration du conteneur sont des symlinks vers le dépôt, un `git pull`
    a donc pu en changer le contenu sans qu'aucun `check()` puisse s'en
    apercevoir. Recharger est sans effet de bord ; l'économiser ferait manquer
    un `pg_hba` modifié. Comme tous les effets, ils ne sont pas joués en
    simulation.
    """
    connues = {getattr(s, "name") for s in steps}
    rapports: list[Report] = []
    par_nom: dict[str, Report] = {}

    for etape in steps:
        for prerequis in getattr(etape, "requires", ()):
            if prerequis not in connues:
                raise KeyError(
                    f"étape « {etape.name} » : dépendance absente « {prerequis} »"
                )

        raison = etape.skip_if(ctx)
        if raison:
            rapport = Report(etape.name, etape.section, SKIP, raison)
            rapports.append(rapport)
            par_nom[etape.name] = rapport
            continue

        # Ce qui n'a pas été posé ne peut pas être constaté. En APPLY la
        # dépendance vient d'être appliquée, donc l'état est observable.
        # Un prérequis qui a ÉCHOUÉ n'a pas eu lieu, quel que soit le mode :
        # ce qui en dépend n'est pas évaluable, et le prétendre serait pire que
        # de l'avouer.
        en_echec = [
            nom for nom in getattr(etape, "requires", ())
            if nom in par_nom and par_nom[nom].state in ("error", BLOCKED, UNKNOWN)
        ]
        en_attente = [
            nom for nom in getattr(etape, "requires", ())
            if nom in par_nom and _reste_a_faire(par_nom[nom])
        ]
        if en_echec or (en_attente and not ctx.mode.applies):
            en_attente = en_echec or en_attente
            rapport = Report(
                etape.name, etape.section, UNKNOWN,
                "non évaluable tant que " + ", ".join(en_attente) + " n'est pas posé",
            )
            rapports.append(rapport)
            par_nom[etape.name] = rapport
            continue

        try:
            resultat = etape.check(ctx)
        except Exception as exc:  # noqa: BLE001 - filet, volontaire
            # Une étape qui lève ne doit pas abattre le parcours : en --status
            # on perdrait le reste du bilan, et c'est justement le moment où
            # l'on veut tout voir. Le défaut est rapporté sur SA ligne.
            rapport = Report(
                etape.name, etape.section, "error",
                f"{type(exc).__name__}: {exc}".splitlines()[0],
            )
            rapports.append(rapport)
            par_nom[etape.name] = rapport
            continue

        rapport = Report(etape.name, etape.section, resultat.state, resultat.detail)

        secretes = [a for a in resultat.actions if a.generates_secret]
        if secretes and not ctx.allow_secrets:
            rapport.state = BLOCKED
            rapport.detail = (
                "génère un secret — à demander explicitement : "
                + ", ".join(a.label for a in secretes)
            )
        elif resultat.actions:
            rapport.applied = _jouer(resultat.actions, ctx)

        rapports.append(rapport)
        par_nom[etape.name] = rapport

    if ctx.mode.applies:
        ctx.request(effets_finaux)
        for effet in ctx.flush():
            info(f"  effet : {effet}")
    else:
        # Ce qui n'a pas été vidé par une barrière se dit ici, sinon
        # l'information se perdrait au lieu d'être annoncée.
        ctx.flush()
    return rapports


class Barrier:
    """Une étape qui ne constate rien : elle VIDE les effets en attente.

    « Redémarrer après tous les `pct set` » est un effet coalescé, pas un ordre
    de déclaration — et il faut bien un endroit où le vider. Cet endroit ne
    peut pas être la fin du parcours : la section suivante observerait alors un
    conteneur d'avant son redémarrage, donc un montage encore vide.

    Elle touche au contexte depuis `check()`, ce que les autres étapes ne font
    pas. La règle qu'elle respecte reste intacte : **rien n'est fait au SYSTÈME
    hors du mode qui applique**. En simulation, `flush()` annonce et n'exécute
    pas — et c'est précisément là que « il faudra redémarrer » se lit.
    """

    def __init__(self, name: str, section: str,
                 requires: Sequence[str] = ()) -> None:
        self.name = name
        self.section = section
        self.requires = tuple(requires)

    def skip_if(self, ctx: "Context") -> str | None:
        return None

    def check(self, ctx: "Context") -> Outcome:
        en_attente = ctx.pending()
        if not en_attente:
            return Outcome("ok", "aucun effet en attente")
        ctx.flush()
        # Le compte rendu suit ce qui a réellement eu lieu : dire « vidés » sur
        # un mode qui ne touche à rien serait rendre compte d'une action qui
        # n'a pas été faite.
        verbe = "effets vidés" if ctx.mode.applies else "effets nécessaires"
        return Outcome("ok", f"{verbe} : " + ", ".join(en_attente))


def _reste_a_faire(rapport: Report) -> bool:
    return rapport.state in ("drift", "absent", UNKNOWN, BLOCKED, "error")


def _jouer(actions: Sequence[Action], ctx: Context) -> tuple[str, ...]:
    if not ctx.mode.applies:
        for action in actions:
            if ctx.mode is Mode.DRY_RUN:
                info(f"  [dry-run] {action.label}")
            # Demandé même sans être joué : c'est ce qui permet d'annoncer le
            # redémarrage que ces poses rendraient nécessaire. Le vidage, lui,
            # n'exécute rien dans ce mode.
            ctx.request(action.effects)
        return ()
    faites: list[str] = []
    for action in actions:
        info(f"  {action.label}")
        action.run(ctx)
        ctx.request(action.effects)
        faites.append(action.label)
    return tuple(faites)


# ─── bilan ───────────────────────────────────────────────────────────────────

# Les trois verdicts du bash, et rien de plus : le résumé se lit d'un coup
# d'œil ou il ne sert à rien.
VERDICTS = {
    "ok": "OK",
    "drift": "POSE",
    "absent": "POSE",
    "error": "KO",
    UNKNOWN: "KO",
    BLOCKED: "KO",
}


def render_summary(rapports: Iterable[Report]) -> str:
    """Une ligne par élément, dans l'ordre d'exécution.

    Les étapes sautées n'y figurent pas : le bash n'en parlait pas non plus, et
    une ligne « sauté » par drapeau noierait les trois verdicts qui comptent.
    """
    lignes = []
    for rapport in rapports:
        if rapport.state == SKIP:
            continue
        lignes.append(f"  {VERDICTS[rapport.state]:<8} {rapport.step}")
    return "\n".join(lignes)


def render_report(rapports: Iterable[Report]) -> str:
    """Le bilan avec ses motifs, pour --status."""
    lignes = []
    for rapport in rapports:
        if rapport.state == SKIP:
            continue
        ligne = f"  {VERDICTS[rapport.state]:<8} {rapport.step}"
        if rapport.detail:
            ligne += f" — {rapport.detail}"
        lignes.append(ligne)
    return "\n".join(lignes)
