"""Section B — la pose dans le conteneur.

TOUT DÉPEND D'UNE SENTINELLE. Un `mpN` n'est pris en compte qu'au DÉMARRAGE du
conteneur. Tant que celui-ci n'a pas redémarré, `/etc/forgejo-git` est un
répertoire vide — sans le moindre message d'erreur — et poser quoi que ce soit
depuis là-dedans copierait du néant. La première étape vérifie donc que le
montage est visible, et toutes les autres en dépendent : le parcours les
déclare non évaluables plutôt que de les laisser conclure dans le vide.

COPIE OU LIEN, SELON QUI ÉCRIT. Les fichiers de configuration de PostgreSQL
sont des **symlinks** vers le montage : personne d'autre que nous ne les
écrit, et ils suivent un `git pull` tout seuls. `app.ini`, lui, est une
**copie** — Forgejo réécrit sa configuration s'il lui manque un secret qu'il
sait générer, et cette écriture sur un lien vers un montage en lecture seule
échoue d'une façon illisible. Les scripts et les unités sont des copies aussi,
parce qu'un montage en lecture seule ne peut pas porter le bit d'exécution.

DEUX RAFRAÎCHISSEMENTS, ET LE PLUS FORT L'EMPORTE. Reposer un symlink de
configuration PostgreSQL demande un **restart** : `listen_addresses` ne se
relit pas à chaud. Le reste se contente d'un **reload**, demandé
systématiquement — les configurations étant des symlinks, leur contenu a pu
changer avec un `git pull` sans qu'aucun `check()` puisse s'en apercevoir.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.commands import Systemd
from core.converge import Action, Outcome
from fjtool.deploy import MP
from proxmox import Container, diff_tree

EFFET_DAEMON_RELOAD = "ct.daemon-reload"
EFFET_PG_RESTART = "ct.postgresql.restart"
EFFET_PG_REFRESH = "ct.postgresql.refresh"
EFFET_FORGEJO_RESTART = "ct.forgejo.restart"

SENTINELLE = "montage /etc/forgejo-git"
PYTHON_CT = "python3-minimal (CT)"
UTILISATEUR = "utilisateur git"


class EtapeCT:
    """Socle : section B, et rien ne se pose sans le montage."""

    section = "B"
    requires: tuple[str, ...] = (SENTINELLE,)

    def skip_if(self, ctx) -> str | None:
        return None

    def _ct(self, ctx):
        return ctx.runner.for_container(ctx.opts.ctid)


class MontageVisible:
    """La sentinelle. Sans elle, tout le reste pose dans le vide.

    Elle interroge `app.ini`, et non un fichier quelconque du montage : c'est
    celui dont l'absence a le plus de conséquences, et le voir prouve à la fois
    que le montage est pris en compte et qu'il pointe sur le bon répertoire.
    """

    name = SENTINELLE
    section = "B"
    requires: tuple[str, ...] = ()

    def skip_if(self, ctx) -> str | None:
        return None

    def check(self, ctx) -> Outcome:
        vu = ctx.runner.for_container(ctx.opts.ctid).probe(
            "test", "-f", f"{MP}/app.ini"
        )
        if vu:
            return Outcome("ok", MP)
        return Outcome(
            "error",
            f"{MP} absent du CT {ctx.opts.ctid} — un point de montage n'est lu "
            f"qu'au démarrage : pct reboot {ctx.opts.ctid}",
        )


class MontageLectureSeule(EtapeCT):
    """Le montage est-il RÉELLEMENT en lecture seule, vu du conteneur ?

    `ro=1` dans `pct config` dit ce qui a été demandé ; cette étape dit ce qui
    a été obtenu. Les deux ont divergé au moins une fois dans la vie de ce
    dépôt — un `pct set` passé sans redémarrage —, et c'est le genre d'écart
    qu'on ne voit que le jour où quelque chose a écrit dans le dépôt.

    Le contrôle se fait par LECTURE de /proc/mounts, jamais en tentant une
    écriture : une protection se lit, elle ne s'éprouve pas en écrivant.
    """

    name = "montage en lecture seule"

    def check(self, ctx) -> Outcome:
        # Script CONSTANT, chemin en argument : rien n'est concaténé.
        res = self._ct(ctx).read(
            "sh", "-c",
            'awk -v m="$1" \'$2 == m { print $4 }\' /proc/mounts',
            "sh", MP,
            check=False,
        )
        options = res.out
        if not options:
            return Outcome("error", f"{MP} n'apparaît pas dans /proc/mounts")
        if options.split(",")[0] == "ro":
            return Outcome("ok", f"{MP} {options}")
        return Outcome(
            "error",
            f"{MP} monté en {options.split(',')[0]} — attendu ro ; "
            f"le conteneur peut réécrire sa propre configuration et son "
            f"épinglage de version. Corriger : pct set {ctx.opts.ctid} "
            f"--mp1 <source>,mp={MP},ro=1 puis redémarrer",
        )


class PaquetCT(EtapeCT):
    """Un paquet du conteneur, constaté par la présence de son binaire.

    Rien ne garantit le contenu d'un conteneur recréé autrement que par ce
    déploiement, et l'absence ne se voit qu'au moment où quelque chose échoue.
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
                f"{self.paquet} absent et --no-install",
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


class UtilisateurGit(EtapeCT):
    """L'utilisateur système sous lequel Forgejo tourne.

    `--system` : pas de compte interactif, pas de mot de passe, un UID dans la
    plage système. Le home est réel (`/home/git`) parce que git-lfs et le
    serveur SSH interne y déposent des fichiers d'état, et qu'un home
    inexistant produit des erreurs qui ne le nomment pas.

    Le shell est `/bin/bash` et non `/usr/sbin/nologin` : Forgejo exécute des
    hooks git sous cet utilisateur.
    """

    name = UTILISATEUR
    requires: tuple[str, ...] = (SENTINELLE,)

    def check(self, ctx) -> Outcome:
        res = self._ct(ctx).read("id", "-u", "git", check=False)
        if res.ok:
            return Outcome("ok", f"uid {res.out}")
        return Outcome(
            "absent",
            "l'utilisateur git n'existe pas — Forgejo ne peut pas démarrer",
            (
                Action(
                    "adduser --system --shell /bin/bash --group "
                    "--disabled-password --home /home/git git (CT)",
                    lambda c: c.runner.for_container(c.opts.ctid).write(
                        "adduser", "--system", "--shell", "/bin/bash",
                        "--group", "--disabled-password",
                        "--home", "/home/git", "git"),
                ),
            ),
        )


class Repertoire(EtapeCT):
    """Un répertoire du conteneur, avec son propriétaire et son mode.

    Les trois comptent ensemble : `/etc/forgejo/secrets` en 0755 laisserait
    n'importe quel processus du conteneur lire la clé qui chiffre les jetons
    d'accès, et rien ne le signalerait.
    """

    requires: tuple[str, ...] = (SENTINELLE, UTILISATEUR)

    def __init__(self, chemin: str, proprietaire: str, mode: str) -> None:
        self.chemin = chemin
        self.proprietaire = proprietaire
        self.mode = mode
        self.name = chemin

    def check(self, ctx) -> Outcome:
        # Un seul aller-retour : le mode et le propriétaire ensemble. Les
        # demander séparément ferait deux `pct exec` pour une seule question.
        res = self._ct(ctx).read(
            "sh", "-c", 'stat -c "%a %U:%G" "$1" 2>/dev/null || true',
            "sh", self.chemin,
            check=False,
        )
        attendu = f"{self.mode} {self.proprietaire}"
        if res.out == attendu:
            return Outcome("ok", attendu)
        return Outcome(
            "drift" if res.out else "absent",
            f"{res.out or 'absent'} → attendu {attendu}",
            (
                Action(
                    f"install -d -m {self.mode} -o {self.proprietaire.split(':')[0]} "
                    f"-g {self.proprietaire.split(':')[1]} {self.chemin} (CT)",
                    lambda c, ch=self.chemin, p=self.proprietaire, m=self.mode:
                        c.runner.for_container(c.opts.ctid).write(
                            "install", "-d", "-m", m,
                            "-o", p.split(":")[0], "-g", p.split(":")[1], ch),
                ),
            ),
        )


class ClusterDetecte(EtapeCT):
    """Où vit la configuration PostgreSQL. Découvert, jamais codé en dur.

    Debian 13 livre PostgreSQL 17 ; la majeure suivante déplacera le
    répertoire. Le déduire de `pg_lsclusters` évite d'y penser ce jour-là.
    """

    name = "cluster PostgreSQL"
    # `pg_lsclusters` vient du paquet : l'interroger avant l'installation
    # rendrait « aucun cluster » là où la vraie réponse est « pas encore ».
    requires = (SENTINELLE, "postgresql (CT)")

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
            return Outcome("error", f"plusieurs clusters, cible ambiguë : {noms}")
        version, nom = clusters[0]
        chemin = f"/etc/postgresql/{version}/{nom}"
        ctx.facts["cluster_dir"] = chemin
        ctx.facts["cluster_version"] = version
        return Outcome("ok", f"{version}/{nom} ({chemin})")


class SymlinkConf(EtapeCT):
    """Un fichier de configuration PostgreSQL, lié au montage plutôt que copié.

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
                    effects=frozenset({EFFET_PG_RESTART}),
                ),
            ),
        )


class FichierCT(EtapeCT):
    """Un script ou une unité, COPIÉ depuis le montage vers le conteneur.

    `install` et non `ln` : le montage est en lecture seule et ne peut pas
    porter le bit d'exécution. La comparaison se fait dans le conteneur, en un
    seul aller-retour.
    """

    def __init__(
        self,
        nom: str,
        cible: str,
        mode: int,
        *,
        proprietaire: str = "root:root",
        effets: frozenset[str] = frozenset({EFFET_DAEMON_RELOAD}),
        requires: tuple[str, ...] = (SENTINELLE,),
    ) -> None:
        self.nom = nom
        self.cible = cible
        self.mode = mode
        self.proprietaire = proprietaire
        self.effets = effets
        self.requires = requires
        self.name = nom

    def check(self, ctx) -> Outcome:
        source = f"{MP}/{self.nom}"
        # Contenu, mode ET propriétaire dans le même aller-retour. Comparer le
        # seul contenu laisserait passer un app.ini lisible par tout le monde.
        conforme = self._ct(ctx).probe(
            "sh", "-c",
            'cmp -s "$1" "$2" && [ "$(stat -c "%a %U:%G" "$2")" = "$3 $4" ]',
            "sh", source, self.cible, f"{self.mode:o}", self.proprietaire,
        )
        if conforme:
            return Outcome("ok", f"{self.cible} ({self.mode:o} {self.proprietaire})")
        proprio = self.proprietaire.split(":")
        return Outcome(
            "drift",
            self.cible,
            (
                Action(
                    f"install -m {self.mode:o} -o {proprio[0]} -g {proprio[1]} "
                    f"{source} {self.cible} (CT)",
                    lambda c, s=source, d=self.cible, m=self.mode, p=proprio:
                        c.runner.for_container(c.opts.ctid).write(
                            "install", "-m", f"{m:o}", "-o", p[0], "-g", p[1],
                            s, d),
                    effects=self.effets,
                ),
            ),
        )


class TimerSauvegardeArme(EtapeCT):
    """`fj-backup.timer`, armé dans le conteneur.

    Contrairement au hors-site, aucun prérequis externe ne conditionne son
    armement : une sauvegarde locale n'a besoin ni de clé ni de réseau.
    """

    name = "fj-backup.timer (armement)"
    # L'unité ET le moteur qu'elle appelle : armer un timer dont l'ExecStart
    # n'existe pas produit un échec par nuit, à 2h45, que personne ne regarde.
    requires = (SENTINELLE, "fj-backup.timer", "fj (CT)")

    def check(self, ctx) -> Outcome:
        systemd = Systemd(self._ct(ctx))
        if systemd.is_enabled("fj-backup.timer"):
            return Outcome("ok", "actif")
        return Outcome(
            "absent",
            "inactif — la source de vérité reste sans filet",
            (
                Action(
                    "systemctl enable --now fj-backup.timer (CT)",
                    lambda c: Systemd(
                        c.runner.for_container(c.opts.ctid)
                    ).enable_now("fj-backup.timer"),
                ),
            ),
        )


class ServiceForgejoArme(EtapeCT):
    """`forgejo.service`, activé et démarré.

    Dépend de tout le reste : sans binaire, sans base et sans secrets, le
    démarrer ne ferait qu'ajouter des lignes d'échec au journal.
    """

    name = "forgejo (armement)"
    requires = (SENTINELLE, "forgejo.service", "base forgejo",
                "secrets Forgejo")

    def check(self, ctx) -> Outcome:
        systemd = Systemd(self._ct(ctx))
        actif = systemd.is_active("forgejo")
        arme = systemd.is_enabled("forgejo")
        if actif and arme:
            return Outcome("ok", "active, enabled")
        actions = []
        if not arme or not actif:
            actions.append(
                Action(
                    "systemctl enable --now forgejo (CT)",
                    lambda c: Systemd(
                        c.runner.for_container(c.opts.ctid)
                    ).enable_now("forgejo"),
                )
            )
        return Outcome(
            "absent" if not arme else "drift",
            f"active={actif}, enabled={arme}",
            tuple(actions),
        )


# ─── le moteur Python, poussé et non monté ───────────────────────────────────


def _empreinte(chemin: Path) -> str:
    return hashlib.sha256(chemin.read_bytes()).hexdigest()


class MoteurCT:
    """L'arbre d'import de `fj` DANS le conteneur : `core` et `fjtool`.

    **Poussé par `pct push`, jamais monté.** Le montage est vivant : un
    `git pull` pendant qu'une sauvegarde tourne à 2h45 livrerait un arbre à
    moitié à jour, ou un `ImportError` sur un module supprimé en cours
    d'exécution.

    **Le conteneur ne reçoit jamais `proxmox`.** Il n'a pas `pct` et n'a rien à
    en faire.

    **Ce qui n'est plus dans le dépôt est RETIRÉ.** Sans élagage, un module
    renommé laisse son ancêtre en place, et cet ancêtre continue de s'importer.
    """

    name = "moteur (CT)"
    section = "B"
    requires: tuple[str, ...] = (PYTHON_CT,)

    def skip_if(self, ctx) -> str | None:
        return None

    def _sources(self, ctx) -> dict[str, Path]:
        """Chemin relatif → fichier source. DEUX paquets, pas trois."""
        racines = [ctx.paths.lib_src / "core", ctx.paths.fjtool_src]
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
                    pousser(c, s, f"{d}/{r}"),
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


def pousser(ctx, source: Path, cible: str, perms: str = "0644") -> None:
    """`pct push` ne crée pas les répertoires intermédiaires."""
    conteneur = Container(ctx.runner, ctx.opts.ctid)
    parent = cible.rsplit("/", 1)[0]
    conteneur.exec("mkdir", "-p", parent)
    conteneur.push(source, cible, perms=perms)


class LanceurCT:
    """`/usr/local/bin/fj` dans le conteneur.

    Poussé en 0755 : le lanceur n'est PAS dans `ct/`, il vit à la racine du
    service, et un montage en lecture seule ne porterait de toute façon pas le
    bit d'exécution.
    """

    name = "fj (CT)"
    section = "B"
    requires: tuple[str, ...] = (PYTHON_CT, "moteur (CT)")

    def skip_if(self, ctx) -> str | None:
        return None

    def check(self, ctx) -> Outcome:
        source = ctx.paths.launcher
        cible = str(ctx.paths.ct_fj)
        conteneur = Container(ctx.runner, ctx.opts.ctid)
        # Le mode part avec l'empreinte : un fichier juste mais non exécutable
        # ne se voit qu'à l'usage.
        vu = conteneur.exec(
            "sh", "-c",
            'sha256sum "$1" 2>/dev/null | cut -d" " -f1; stat -c %a "$1" 2>/dev/null',
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
                    lambda c, s=source, d=cible: pousser(c, s, d, "0755"),
                ),
            ),
        )
