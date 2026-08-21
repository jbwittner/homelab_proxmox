"""Section B — la pose dans le conteneur.

TOUT DÉPEND D'UNE SENTINELLE. Un `mpN` n'est pris en compte qu'au DÉMARRAGE du
conteneur. Tant que celui-ci n'a pas redémarré, `/etc/forgejo-git` est un
répertoire vide — sans le moindre message d'erreur — et poser quoi que ce soit
depuis là-dedans copierait du néant. La première étape vérifie donc que le
montage est visible, et toutes les autres en dépendent : le parcours les
déclare non évaluables plutôt que de les laisser conclure dans le vide.

RIEN N'EST LIÉ AU MONTAGE, TOUT EN EST COPIÉ. Les unités systemd parce qu'un
montage en lecture seule ne porte pas le bit d'exécution ; `app.ini` pour deux
raisons qui se renforcent :

  - Forgejo réécrit sa configuration s'il lui manque un secret qu'il sait
    générer, et cette écriture sur un lien vers un montage en lecture seule
    échoue d'une façon illisible ;
  - `app.ini` n'est plus une copie conforme du dépôt de toute façon : le mot
    de passe de la base y est SUBSTITUÉ à la pose (voir `AppIni`). Le fichier
    servi ne peut donc pas exister dans le dépôt.

Conséquence à retenir : **un `git pull` ne suffit jamais**, il faut rejouer
`fj deploy`. C'est le geste normal de ce dépôt.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.commands import Systemd
from core.converge import Action, Outcome
from fjtool.deploy import MP
from proxmox import Container

EFFET_DAEMON_RELOAD = "ct.daemon-reload"
EFFET_FORGEJO_RESTART = "ct.forgejo.restart"

SENTINELLE = "montage /etc/forgejo-git"
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


class AppIni(EtapeCT):
    """`/etc/forgejo/app.ini`, RENDU depuis le gabarit du dépôt.

    Une seule valeur est substituée — le mot de passe de la base — et c'est
    justement celle qui ne peut pas vivre dans un dépôt. Tout le reste vient
    de `ct/app.ini` tel quel.

    LE SECRET NE PASSE NI PAR UN ARGV NI PAR UN FICHIER DU NŒUD. Il est lu
    dans le conteneur, le rendu s'y fait aussi, et le tout tient dans un seul
    `sh -c` dont le script est CONSTANT. Un `ps` pendant l'opération ne montre
    donc rien, et rien n'est écrit hors de `/etc/forgejo`.

    LA COMPARAISON PORTE SUR LE RÉSULTAT, pas sur le gabarit : c'est ce qui
    permet à « zéro modification sur un état conforme » de tenir alors même
    que le fichier servi ne peut être identique à aucun fichier du dépôt.
    """

    name = "app.ini"
    requires = (SENTINELLE, "/etc/forgejo", "mot de passe de la base")

    CIBLE = "/etc/forgejo/app.ini"
    MARQUEUR = "@@DB_PASSWORD@@"

    # Le rendu et sa comparaison, en un aller-retour. `$1` gabarit, `$2` mot
    # de passe, `$3` cible. `awk` avec une variable passée par -v : le mot de
    # passe ne traverse jamais une expression rationnelle, donc aucun
    # caractère ne peut y changer le sens du remplacement.
    _RENDU = (
        'awk -v p="$(cat "$2")" '
        '\'{ gsub(/@@DB_PASSWORD@@/, p); print }\' "$1"'
    )

    def _empreinte_voulue(self, ctx) -> str:
        ct = self._ct(ctx)
        from fjtool.steps.postgres import MOT_DE_PASSE

        return ct.read(
            "sh", "-c", f"{self._RENDU} | sha256sum | cut -d' ' -f1",
            "sh", f"{MP}/app.ini", MOT_DE_PASSE,
            check=False,
        ).out

    def check(self, ctx) -> Outcome:
        ct = self._ct(ctx)
        voulue = self._empreinte_voulue(ctx)
        if not voulue:
            return Outcome(
                "error",
                f"impossible de rendre {MP}/app.ini — le gabarit ou le mot de "
                "passe manque",
            )

        vue = ct.read(
            "sh", "-c",
            'sha256sum "$1" 2>/dev/null | cut -d" " -f1; '
            'stat -c "%a %U:%G" "$1" 2>/dev/null',
            "sh", self.CIBLE,
            check=False,
        ).lines

        if vue[:2] == [voulue, "640 root:git"]:
            return Outcome("ok", f"{self.CIBLE} (640 root:git)")

        return Outcome(
            "drift" if vue else "absent",
            f"{self.CIBLE} — mot de passe de la base substitué",
            (
                Action(
                    f"rendre {MP}/app.ini → {self.CIBLE} (640 root:git)",
                    _rendre_app_ini,
                    effects=frozenset({EFFET_FORGEJO_RESTART}),
                ),
            ),
        )


def _rendre_app_ini(ctx) -> None:
    from fjtool.steps.postgres import MOT_DE_PASSE

    ct = ctx.runner.for_container(ctx.opts.ctid)
    # umask AVANT la redirection : créer en 0644 puis corriger laisserait une
    # fenêtre où le mot de passe de la base est lisible par tout le conteneur.
    ct.write(
        "sh", "-c",
        f'umask 027 && {AppIni._RENDU} > "$3"',
        "sh", f"{MP}/app.ini", MOT_DE_PASSE, AppIni.CIBLE,
    )
    ct.write("chown", "root:git", AppIni.CIBLE)
    ct.write("chmod", "0640", AppIni.CIBLE)


class ServiceForgejoArme(EtapeCT):
    """`forgejo.service`, activé et démarré.

    Dépend de tout le reste : sans binaire, sans base et sans secrets, le
    démarrer ne ferait qu'ajouter des lignes d'échec au journal.
    """

    name = "forgejo (armement)"
    requires = (SENTINELLE, "forgejo.service", "app.ini",
                "connexion à la base (CT 200)", "secrets Forgejo")

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


def pousser(ctx, source: Path, cible: str, perms: str = "0644") -> None:
    """`pct push` ne crée pas les répertoires intermédiaires."""
    conteneur = Container(ctx.runner, ctx.opts.ctid)
    parent = cible.rsplit("/", 1)[0]
    conteneur.exec("mkdir", "-p", parent)
    conteneur.push(source, cible, perms=perms)


