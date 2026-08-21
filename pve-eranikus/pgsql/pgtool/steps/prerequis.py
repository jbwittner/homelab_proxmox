"""Section A — prérequis du conteneur. La plus délicate du déploiement.

Elle est la seule à toucher aux DISQUES et à la PROTECTION, et la seule à
provoquer un redémarrage. Quatre pièges de production y sont encodés.

**La protection interdit toute modification de disque**, ajout de point de
montage compris. Il faut la lever, faire, et la remettre — et ne pas la
remettre ne produit aucune erreur, ne se voit pas, et laisse le conteneur qui
porte les données de tous les services sans son garde-fou. D'où un
gestionnaire de contexte avec un `finally`, jamais une paire d'appels.

**Un `mpN` n'est pris en compte qu'au démarrage.** Poser sans redémarrer donne
un répertoire vide côté conteneur, silencieusement. Chaque pose déclare donc
l'effet `ct.reboot`, que le parcours coalesce et vide après toutes les
étapes — c'est ainsi que « redémarrer après tous les `pct set` » s'exprime.

**`nesting=1` est obligatoire sur Debian 13.** Sans lui, les unités qui
montent un tmpfs pour les credentials systemd — ce que fait `pg-backup.service`
avec `PrivateTmp` — échouent en `243/CREDENTIALS`, et le conteneur démarre en
état dégradé sans que rien ne le signale.

**Un `mp2` monté ailleurs ne se déplace pas.** Il porte des données. On le
signale, on pose le fait « divergent », et le hors-site refusera de s'armer.
Corriger serait pire que constater.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from core.converge import Action, Outcome
from core.log import info
from pgtool.deploy import MP
from proxmox import Container, MountPoint

EFFET_REBOOT = "ct.reboot"

CT_DEMARRE = "CT démarré"


class EtapeA:
    """Socle : section A, sautée par `--no-container`."""

    section = "A"
    requires: tuple[str, ...] = (CT_DEMARRE,)

    def skip_if(self, ctx) -> str | None:
        if not ctx.opts.do_container:
            return "--no-container"
        return None

    def _ct(self, ctx) -> Container:
        return Container(ctx.runner, ctx.opts.ctid)


class Protection:
    """La protection du conteneur, et le moyen de la lever sans l'oublier.

    Sert à deux titres : c'est une étape du bilan, et c'est le gestionnaire de
    contexte que les poses de points de montage utilisent.
    """

    name = "protection"
    section = "A"
    requires: tuple[str, ...] = (CT_DEMARRE,)

    def __init__(self, ctid: int | None = None) -> None:
        self.ctid = ctid

    def skip_if(self, ctx) -> str | None:
        if not ctx.opts.do_container:
            return "--no-container"
        return None

    @contextmanager
    def levee(self, ctx) -> Iterator[None]:
        """Lève la protection et la REMET, y compris sur exception.

        Le `finally` n'est pas une précaution de style : ne pas remettre la
        protection ne produit aucune erreur et ne se voit nulle part.
        """
        conteneur = Container(ctx.runner, self.ctid or ctx.opts.ctid)
        protege = conteneur.config().get("protection") == "1"
        if not protege:
            yield
            return
        info(f"  levée temporaire de la protection du CT {conteneur.ctid}")
        conteneur.set(protection=0)
        try:
            yield
        finally:
            if conteneur.config().get("protection") != "1":
                conteneur.set(protection=1)
                info(f"  protection du CT {conteneur.ctid} rétablie")

    def check(self, ctx) -> Outcome:
        conteneur = Container(ctx.runner, self.ctid or ctx.opts.ctid)
        if conteneur.config().get("protection") == "1":
            return Outcome("ok", "activée")
        return Outcome(
            "drift",
            "désactivée — ce conteneur porte les données de tous les services",
            (
                Action(
                    f"pct set {conteneur.ctid} --protection 1",
                    lambda c: Container(
                        c.runner, self.ctid or c.opts.ctid).set(protection=1),
                ),
            ),
        )


class ConteneurDemarre:
    """Rien ne se constate dans un conteneur arrêté."""

    name = CT_DEMARRE
    section = "A"
    requires: tuple[str, ...] = ()

    def skip_if(self, ctx) -> str | None:
        if not ctx.opts.do_container:
            return "--no-container"
        return None

    def check(self, ctx) -> Outcome:
        conteneur = Container(ctx.runner, ctx.opts.ctid)
        if conteneur.running:
            return Outcome("ok", "running")
        return Outcome(
            "absent",
            f"CT {ctx.opts.ctid} à l'arrêt",
            (
                Action(
                    f"pct start {ctx.opts.ctid}",
                    lambda c: Container(c.runner, c.opts.ctid).start(),
                    effects=frozenset({EFFET_REBOOT}),
                ),
            ),
        )


class Nesting(EtapeA):
    """`nesting=1`, obligatoire sur Debian 13.

    La feature est ajoutée en PRÉSERVANT les autres : écraser la liste
    retirerait `keyctl` ou ce que le script de création aura posé.
    """

    name = "nesting"

    def check(self, ctx) -> Outcome:
        features = self._ct(ctx).features()
        if "nesting=1" in features:
            return Outcome("ok", ",".join(sorted(features)))
        etat = "drift" if any(f.startswith("nesting=") for f in features) else "absent"
        return Outcome(
            etat,
            "sans nesting=1, les unités à PrivateTmp échouent en 243/CREDENTIALS",
            (
                Action(
                    f"pct set {ctx.opts.ctid} --features nesting=1 (les autres conservées)",
                    _poser_nesting,
                    effects=frozenset({EFFET_REBOOT}),
                ),
            ),
        )


def _poser_nesting(ctx) -> None:
    conteneur = Container(ctx.runner, ctx.opts.ctid)
    with Protection().levee(ctx):
        conteneur.ensure_feature("nesting=1")


class PointDeMontage(EtapeA):
    """Socle des deux `mpN`. Toute pose passe par la levée de protection et
    déclare qu'un redémarrage sera nécessaire."""

    def _voulu(self, ctx) -> MountPoint:
        raise NotImplementedError

    def _action(self, ctx, mp: MountPoint) -> Action:
        return Action(
            f"pct set {ctx.opts.ctid} --{mp.key} {mp.render()}",
            lambda c, voulu=mp: _poser_montage(c, voulu),
            effects=frozenset({EFFET_REBOOT}),
        )


def _poser_montage(ctx, mp: MountPoint) -> None:
    conteneur = Container(ctx.runner, ctx.opts.ctid)
    with Protection().levee(ctx):
        conteneur.set(**{mp.key: mp.render()})


class Mp1Depot(PointDeMontage):
    """Le montage du dépôt : `ct/` seul, en lecture seule.

    La source est `ct/` et non le répertoire du service : le conteneur n'a pas
    à voir les scripts du nœud, le nom du bucket ni le chemin de la clé.
    """

    name = "mp1"

    def _voulu(self, ctx) -> MountPoint:
        return MountPoint(1, str(ctx.paths.ct_src), MP, readonly=True)

    def check(self, ctx) -> Outcome:
        voulu = self._voulu(ctx)
        actuel = self._ct(ctx).config().get(voulu.key)
        if voulu.matches(actuel):
            return Outcome("ok", f"{voulu.source} → {MP}")
        etat = "drift" if actuel else "absent"
        return Outcome(
            etat,
            f"{actuel or 'absent'} → attendu {voulu.render()}",
            (self._action(ctx, voulu),),
        )


class Mp2Sauvegardes(PointDeMontage):
    """Le volume des sauvegardes, sur un disque DISTINCT de celui de PGDATA.

    C'est toute la raison d'être de ce second montage : une panne du disque qui
    porte la base ne doit pas emporter les dumps avec elle.

    S'il est monté AILLEURS, on n'y touche pas — il porte des données. On pose
    le fait « divergent », et le hors-site refusera de s'armer dessus.
    """

    name = "mp2"

    def check(self, ctx) -> Outcome:
        actuel = self._ct(ctx).config().get("mp2", "")
        cible = ctx.opts.mp2_mount

        if not actuel:
            voulu = MountPoint(
                2, f"{ctx.opts.mp2_storage}:{ctx.opts.mp2_size}", cible,
                backup=False,
            )
            return Outcome(
                "absent",
                f"aucun volume de sauvegarde — {ctx.opts.mp2_size} Go à créer",
                (self._action(ctx, voulu),),
            )

        # Comparaison par SOUS-CHAÎNE : Proxmox réécrit la spécification en
        # volid généré (`data:subvol-200-disk-0`), on ne peut donc pas comparer
        # à ce qu'on avait demandé.
        if f"mp={cible}" not in actuel:
            ctx.facts["mp2_state"] = "divergent"
            return Outcome(
                "error",
                f"monté ailleurs : {actuel} — il porte des données, "
                "on n'y touche pas ; le hors-site ne sera pas armé",
            )

        ctx.facts["mp2_state"] = "ok"
        detail = f"{actuel}"
        if "backup=0" not in actuel:
            detail += " — sans backup=0, les vzdump emporteront les dumps"
        return Outcome("ok", detail)


class Startup(EtapeA):
    """L'ordre de démarrage au boot du nœud.

    N'importe quelle valeur convient : le but est qu'il y en ait une, pour
    qu'un service qui dépend de cette base démarre après elle.
    """

    name = "startup"

    def check(self, ctx) -> Outcome:
        actuel = self._ct(ctx).config().get("startup", "")
        if actuel:
            return Outcome("ok", actuel)
        return Outcome(
            "absent",
            "aucun ordre de démarrage",
            (
                Action(
                    f"pct set {ctx.opts.ctid} --startup order=1",
                    lambda c: Container(c.runner, c.opts.ctid).set(
                        startup="order=1"),
                ),
            ),
        )
