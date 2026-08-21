"""Section B — la pose dans le conteneur.

TOUT DÉPEND D'UNE SENTINELLE. Un `mpN` n'est pris en compte qu'au DÉMARRAGE du
conteneur. Tant que celui-ci n'a pas redémarré, `/etc/pgsql-git` est un
répertoire vide — sans le moindre message d'erreur — et poser quoi que ce soit
depuis là-dedans copierait du néant. La première étape vérifie donc que le
montage est visible, et toutes les autres en dépendent : le parcours les
déclare non évaluables plutôt que de les laisser conclure dans le vide.

COPIE OU LIEN, SELON LA NATURE DU FICHIER. Les fichiers de configuration sont
des **symlinks** vers le montage : ils suivent un `git pull` tout seuls. Les
scripts et les unités sont des **copies**, parce qu'un montage en lecture seule
ne peut pas porter le bit d'exécution — et parce qu'une copie isole le
conteneur d'un dépôt en cours de mise à jour.

DEUX RAFRAÎCHISSEMENTS, ET LE PLUS FORT L'EMPORTE. Reposer un symlink de
configuration demande un **restart** : `listen_addresses` ne se relit pas à
chaud. Le reste se contente d'un **reload**, demandé systématiquement — les
configurations étant des symlinks, leur contenu a pu changer avec un `git pull`
sans qu'aucun `check()` puisse s'en apercevoir.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.commands import Systemd
from core.converge import Action, Outcome
from pgtool.deploy import MP
from proxmox import Container, diff_tree

EFFET_DAEMON_RELOAD = "ct.daemon-reload"
EFFET_RESTART = "ct.postgresql.restart"
EFFET_REFRESH = "ct.postgresql.refresh"

SENTINELLE = "montage /etc/pgsql-git"


class EtapeCT:
    """Socle : section B, et rien ne se pose sans le montage."""

    section = "B"
    requires: tuple[str, ...] = (SENTINELLE,)

    def skip_if(self, ctx) -> str | None:
        return None

    def _ct(self, ctx):
        return ctx.runner.for_container(ctx.opts.ctid)


class MontageVisible:
    """La sentinelle. Sans elle, tout le reste pose dans le vide."""

    name = SENTINELLE
    section = "B"
    requires: tuple[str, ...] = ()

    def skip_if(self, ctx) -> str | None:
        return None

    def check(self, ctx) -> Outcome:
        vu = ctx.runner.for_container(ctx.opts.ctid).probe(
            "test", "-f", f"{MP}/pg-backup.sh"
        )
        if vu:
            return Outcome("ok", MP)
        return Outcome(
            "error",
            f"{MP} absent du CT {ctx.opts.ctid} — un point de montage n'est lu "
            f"qu'au démarrage : pct reboot {ctx.opts.ctid}",
        )


class PaquetCT(EtapeCT):
    """Un paquet du conteneur, constaté par la présence de son binaire.

    L'image du script communautaire les porte déjà ; rien ne le garantit sur un
    conteneur recréé autrement, et l'absence ne se voit qu'au moment où une
    sauvegarde échoue.
    """

    def __init__(self, paquet: str, binaire: str) -> None:
        self.paquet = paquet
        self.binaire = binaire
        self.name = f"{paquet} (CT)"

    def check(self, ctx) -> Outcome:
        if self._ct(ctx).probe("test", "-x", self.binaire):
            return Outcome("ok", self.binaire)
        if not ctx.opts.do_install:
            return Outcome(
                "error",
                f"{self.paquet} absent et --no-install — "
                "la sauvegarde ne fonctionnera pas",
            )
        return Outcome(
            "absent",
            f"{self.binaire} absent",
            (
                Action(
                    f"apt-get update (CT {ctx.opts.ctid})",
                    lambda c: c.runner.for_container(c.opts.ctid).write(
                        "apt-get", "update", "-qq"),
                ),
                Action(
                    f"apt-get install -y -qq {self.paquet} (CT {ctx.opts.ctid})",
                    lambda c, p=self.paquet: c.runner.for_container(
                        c.opts.ctid).write(
                        "env", "DEBIAN_FRONTEND=noninteractive",
                        "apt-get", "install", "-y", "-qq", p),
                ),
            ),
        )


class TimerFstrim(EtapeCT):
    """`fstrim.timer` : sur du LVM-thin, sans lui les blocs libérés ne sont
    jamais rendus au pool, qui est surprovisionné. Un pool saturé arrête net le
    serveur."""

    name = "fstrim.timer (CT)"

    def check(self, ctx) -> Outcome:
        systemd = Systemd(self._ct(ctx))
        if systemd.is_enabled("fstrim.timer"):
            return Outcome("ok", "actif")
        return Outcome(
            "absent",
            "inactif — les blocs libérés ne reviendraient pas au pool",
            (
                Action(
                    "systemctl enable --now fstrim.timer (CT)",
                    lambda c: Systemd(
                        c.runner.for_container(c.opts.ctid)
                    ).enable_now("fstrim.timer"),
                ),
            ),
        )


class ClusterDetecte(EtapeCT):
    """Où vit la configuration. Découvert, jamais codé en dur.

    `/etc/postgresql/18/main` deviendra `/19/main` à la prochaine majeure : le
    déduire de `pg_lsclusters` évite d'y penser ce jour-là.
    """

    name = "cluster PostgreSQL"

    def check(self, ctx) -> Outcome:
        lignes = self._ct(ctx).read("pg_lsclusters", "-h", check=False).lines
        clusters = [ligne.split()[:2] for ligne in lignes if ligne.split()]
        if not clusters:
            return Outcome(
                "error",
                f"aucun cluster PostgreSQL dans le CT {ctx.opts.ctid}",
            )
        if len(clusters) > 1:
            noms = ", ".join("/".join(c) for c in clusters)
            return Outcome(
                "error",
                f"plusieurs clusters, cible ambiguë : {noms}",
            )
        version, nom = clusters[0]
        chemin = f"/etc/postgresql/{version}/{nom}"
        ctx.facts["cluster_dir"] = chemin
        return Outcome("ok", f"{version}/{nom} ({chemin})")


class SymlinkConf(EtapeCT):
    """Un fichier de configuration, lié au montage plutôt que copié.

    Le lien fait suivre un `git pull` sans redéploiement. Reposer ce lien
    demande en revanche un **restart** : `listen_addresses` ne se relit pas à
    chaud, et la première pose serait sans effet.
    """

    requires = (SENTINELLE, "cluster PostgreSQL")

    def __init__(self, nom: str, sous_dossier: str = "") -> None:
        self.nom = nom
        self.sous_dossier = sous_dossier
        self.name = nom

    def _cible(self, ctx) -> str:
        base = ctx.facts["cluster_dir"]
        if self.sous_dossier:
            return f"{base}/{self.sous_dossier}/{self.nom}"
        return f"{base}/{self.nom}"

    def check(self, ctx) -> Outcome:
        cible = self._cible(ctx)
        attendu = f"{MP}/{self.nom}"
        vu = self._ct(ctx).read("readlink", "-f", cible, check=False).out
        if vu == attendu:
            return Outcome("ok", cible)
        etat = "drift" if vu else "absent"
        return Outcome(
            etat,
            f"{cible} → {vu or 'rien'}, attendu {attendu}",
            (
                Action(
                    f"ln -sfn {attendu} {cible} (CT)",
                    lambda c, a=attendu, d=cible: c.runner.for_container(
                        c.opts.ctid).write("ln", "-sfn", a, d),
                    effects=frozenset({EFFET_RESTART}),
                ),
            ),
        )


class FichierCT(EtapeCT):
    """Un script ou une unité, COPIÉ depuis le montage vers le conteneur.

    `install` et non `ln` : le montage est en lecture seule et ne peut pas
    porter le bit d'exécution. La comparaison se fait dans le conteneur, en un
    seul aller-retour.
    """

    def __init__(self, nom: str, cible: str, mode: int) -> None:
        self.nom = nom
        self.cible = cible
        self.mode = mode
        self.name = nom

    def check(self, ctx) -> Outcome:
        source = f"{MP}/{self.nom}"
        # Script CONSTANT, chemins en arguments : rien n'est concaténé.
        conforme = self._ct(ctx).probe(
            "sh", "-c",
            'cmp -s "$1" "$2" && [ "$(stat -c %a "$2")" = "$3" ]',
            "sh", source, self.cible, f"{self.mode:o}",
        )
        if conforme:
            return Outcome("ok", self.cible)
        return Outcome(
            "drift",
            self.cible,
            (
                Action(
                    f"install -m {self.mode:o} {source} {self.cible} (CT)",
                    lambda c, s=source, d=self.cible, m=self.mode:
                        c.runner.for_container(c.opts.ctid).write(
                            "install", "-m", f"{m:o}", s, d),
                    effects=frozenset({EFFET_DAEMON_RELOAD}),
                ),
            ),
        )


class TimerSauvegardeArme(EtapeCT):
    """`pg-backup.timer`, armé dans le conteneur.

    Contrairement au hors-site, aucun prérequis externe ne conditionne son
    armement : une sauvegarde locale n'a besoin ni de clé ni de réseau.
    """

    name = "pg-backup.timer (armement)"

    def check(self, ctx) -> Outcome:
        systemd = Systemd(self._ct(ctx))
        if systemd.is_enabled("pg-backup.timer"):
            return Outcome("ok", "actif")
        return Outcome(
            "absent",
            "inactif — le conteneur reste sans filet",
            (
                Action(
                    "systemctl enable --now pg-backup.timer (CT)",
                    lambda c: Systemd(
                        c.runner.for_container(c.opts.ctid)
                    ).enable_now("pg-backup.timer"),
                ),
            ),
        )


# ─── le moteur Python, poussé et non monté ───────────────────────────────────

PYTHON_CT = "python3-minimal (CT)"


def _empreinte(chemin: Path) -> str:
    return hashlib.sha256(chemin.read_bytes()).hexdigest()


class MoteurCT:
    """L'arbre d'import de `pg` DANS le conteneur : `core` et `pgtool`.

    **Poussé par `pct push`, jamais monté.** Le montage est vivant : un
    `git pull` pendant qu'une sauvegarde tourne à 2h30 livrerait un arbre à
    moitié à jour, ou un `ImportError` sur un module supprimé en cours
    d'exécution. C'est exactement la raison pour laquelle les scripts sont
    copiés et non liés — elle vaut pour la charge utile Python.

    **Le conteneur ne reçoit jamais `proxmox`.** Il n'a pas `pct` et n'a rien à
    en faire ; l'y pousser ferait passer les tests du nœud à un import de
    `proxmox` depuis le moteur, qui n'échouerait que dans le CT.

    **Ce qui n'est plus dans le dépôt est RETIRÉ.** Sans élagage, un module
    renommé laisse son ancêtre en place, et cet ancêtre continue de s'importer.

    Ne dépend pas du montage — il ne lit rien dedans — mais de l'interpréteur :
    sans `python3`, il n'y a rien à poser, et `pgbk.sh` reste le moteur.
    """

    name = "moteur (CT)"
    section = "B"
    requires: tuple[str, ...] = (PYTHON_CT,)

    def skip_if(self, ctx) -> str | None:
        return None

    def _sources(self, ctx) -> dict[str, Path]:
        """Chemin relatif → fichier source. DEUX paquets, pas trois."""
        racines = [ctx.paths.lib_src / "core", ctx.paths.pgtool_src]
        trouves: dict[str, Path] = {}
        for racine in racines:
            if not racine.is_dir():
                continue
            for fichier in sorted(racine.rglob("*.py")):
                rel = f"{racine.name}/{fichier.relative_to(racine)}"
                trouves[rel] = fichier
        return trouves

    def check(self, ctx) -> Outcome:
        sources = self._sources(ctx)
        racine = str(ctx.paths.ct_lib)
        conteneur = Container(ctx.runner, ctx.opts.ctid)
        distant = conteneur.digests(racine)
        local = {rel: _empreinte(chemin) for rel, chemin in sources.items()}
        a_poser, a_retirer = diff_tree(local, distant)

        if not a_poser and not a_retirer:
            return Outcome("ok", f"{racine} — {len(sources)} module(s)")

        actions = [
            Action(
                f"pct push {ctx.opts.ctid} {rel} → {racine}/{rel}",
                lambda c, r=rel, s=sources[rel], d=racine:
                    _pousser(c, s, f"{d}/{r}"),
            )
            for rel in a_poser
        ]
        actions += [
            Action(
                f"rm {racine}/{rel} (CT — absent du dépôt)",
                lambda c, r=rel, d=racine: Container(
                    c.runner, c.opts.ctid).exec("rm", "-f", f"{d}/{r}"),
            )
            for rel in a_retirer
        ]
        etat = "drift" if distant else "absent"
        return Outcome(
            etat,
            f"{len(a_poser)} à pousser, {len(a_retirer)} à retirer",
            tuple(actions),
        )


def _pousser(ctx, source: Path, cible: str, perms: str = "0644") -> None:
    """`pct push` ne crée pas les répertoires intermédiaires."""
    conteneur = Container(ctx.runner, ctx.opts.ctid)
    parent = cible.rsplit("/", 1)[0]
    conteneur.exec("mkdir", "-p", parent)
    conteneur.push(source, cible, perms=perms)


class LanceurCT:
    """`/usr/local/bin/pg` dans le conteneur.

    Poussé en 0755 : le lanceur n'est PAS dans `ct/`, il vit à la racine du
    service, et un montage en lecture seule ne porterait de toute façon pas le
    bit d'exécution.
    """

    name = "pg (CT)"
    section = "B"
    requires: tuple[str, ...] = (PYTHON_CT, "moteur (CT)")

    def skip_if(self, ctx) -> str | None:
        return None

    def check(self, ctx) -> Outcome:
        source = ctx.paths.launcher
        cible = str(ctx.paths.ct_pg)
        conteneur = Container(ctx.runner, ctx.opts.ctid)
        # Script CONSTANT, chemin en argument. Le mode part avec l'empreinte :
        # un fichier juste mais non exécutable ne se voit qu'à l'usage.
        vu = conteneur.exec(
            "sh", "-c",
            'sha256sum "$1" 2>/dev/null | cut -d" " -f1; stat -c %a "$1" '
            '2>/dev/null',
            "sh", cible,
            check=False,
        ).lines
        if vu[:2] == [_empreinte(source), "755"]:
            return Outcome("ok", cible)
        return Outcome(
            "drift" if vu else "absent",
            cible,
            (
                Action(
                    f"pct push {ctx.opts.ctid} {source} {cible} --perms 0755",
                    lambda c, s=source, d=cible: _pousser(c, s, d, "0755"),
                ),
            ),
        )
