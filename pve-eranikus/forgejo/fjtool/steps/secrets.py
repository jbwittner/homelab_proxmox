"""Section G — les secrets, le compte d'administration, la première sauvegarde.

CES ÉTAPES FONT APPARAÎTRE DES SECRETS. Elles ne sont donc jouées ni par
défaut, ni par surprise : chacune est derrière son drapeau, et l'action
qu'elles proposent est marquée `generates_secret`. Le moteur refuse alors de
l'exécuter sans autorisation explicite, et l'étape ressort « bloquée ».

**Un secret qui existe n'est JAMAIS retouché.** Ce n'est pas de la prudence
générale, c'est une nécessité de fonctionnement : `SECRET_KEY` chiffre, dans
la base, les jetons d'accès, les secrets 2FA et les mots de passe des
miroirs. Le faire tourner ne « renouvelle » rien — il rend illisible tout ce
qui a été chiffré avant, définitivement. Un déploiement de routine qui
régénérerait ce fichier détruirait la moitié du contenu de l'instance sans
qu'aucune erreur ne s'affiche.

**Les secrets sont produits par le binaire lui-même** (`forgejo generate
secret`), et non par un `head -c 32 /dev/urandom` maison. `INTERNAL_TOKEN`
n'est pas une chaîne aléatoire : c'est un jeton signé dont Forgejo attend une
forme précise. En fabriquer un à la main marche jusqu'au jour où le format
change.

**Ils s'affichent UNE fois.** Ils doivent aller dans OpenBao immédiatement :
sans `SECRET_KEY`, un conteneur reconstruit ne peut plus déchiffrer la base
qu'il vient pourtant de restaurer. C'est le scénario de reprise qui échoue le
plus silencieusement, et le seul remède est de l'avoir rangé avant.
"""

from __future__ import annotations

from core.commands import Systemd
from core.converge import Action, Outcome
from core.log import info, warn
from core.runner import Secret
from fjtool.deploy import CT_BINAIRE, CT_SECRETS
from fjtool.steps.conteneur import EFFET_FORGEJO_RESTART, SENTINELLE

# Nom du fichier → nom du secret tel que `forgejo generate secret` l'attend.
# Le fichier porte le nom que la clé `*_URI` d'app.ini désigne ; les deux
# colonnes sont différentes exprès, et les confondre casse silencieusement.
SECRETS = {
    "secret_key": "SECRET_KEY",
    "internal_token": "INTERNAL_TOKEN",
    "oauth2_jwt_secret": "JWT_SECRET",
    "lfs_jwt_secret": "LFS_JWT_SECRET",
}

NOM_SECRETS = "secrets Forgejo"


class EtapeG:
    section = "G"
    requires: tuple[str, ...] = (SENTINELLE,)

    def skip_if(self, ctx) -> str | None:
        return None

    def _ct(self, ctx):
        return ctx.runner.for_container(ctx.opts.ctid)


class SecretsForgejo(EtapeG):
    """Les quatre fichiers de `/etc/forgejo/secrets/`.

    Quatre, et pas deux : `SECRET_KEY` et `INTERNAL_TOKEN` sont ceux que le
    brief nomme, mais Forgejo GÉNÈRE AUSSI `JWT_SECRET` et `LFS_JWT_SECRET`
    s'ils manquent — en réécrivant `app.ini` pour les y ranger. Sur une
    configuration versionnée, cette écriture est exactement ce qu'on ne veut
    pas. Les pré-déposer tous les quatre est ce qui rend le montage en lecture
    seule tenable.
    """

    name = NOM_SECRETS
    requires = (SENTINELLE, "binaire Forgejo", CT_SECRETS)

    def check(self, ctx) -> Outcome:
        ct = self._ct(ctx)
        # Un seul aller-retour pour les quatre : le script est CONSTANT et le
        # répertoire arrive en argument.
        presents = set(
            ct.read(
                "sh", "-c",
                'cd "$1" 2>/dev/null && ls -1 2>/dev/null || true',
                "sh", CT_SECRETS,
                check=False,
            ).lines
        )
        manquants = [nom for nom in SECRETS if nom not in presents]

        if not manquants:
            return Outcome("ok", f"{len(SECRETS)} secrets dans {CT_SECRETS}")

        return Outcome(
            "absent",
            f"manquant(s) : {', '.join(manquants)} — sans eux Forgejo "
            "réécrirait app.ini, qui vient d'un montage en lecture seule",
            tuple(
                Action(
                    f"forgejo generate secret {SECRETS[nom]} → {CT_SECRETS}/{nom}",
                    lambda c, n=nom: _poser_secret(c, n),
                    generates_secret=True,
                    effects=frozenset({EFFET_FORGEJO_RESTART}),
                )
                for nom in manquants
            ),
        )


def _poser_secret(ctx, nom: str) -> None:
    """Génère, écrit en 0640 root:git, et affiche UNE fois.

    Le secret ne passe jamais par un argv : il est produit sur la sortie
    standard de `forgejo generate`, et réinjecté sur l'ENTRÉE standard du
    `cat` qui l'écrit. Un `ps` pendant l'opération ne montre donc rien, et
    aucune trace d'erreur ne peut le recopier.
    """
    ct = ctx.runner.for_container(ctx.opts.ctid)
    valeur = ct.read(CT_BINAIRE, "generate", "secret", SECRETS[nom]).out
    if not valeur:
        raise RuntimeError(
            f"forgejo generate secret {SECRETS[nom]} n'a rien produit"
        )

    cible = f"{CT_SECRETS}/{nom}"
    # umask AVANT la redirection : créer en 0644 puis corriger laisserait une
    # fenêtre, courte mais réelle, où le secret est lisible par tous.
    ct.write(
        "sh", "-c", 'umask 027 && cat > "$1"', "sh", cible,
        stdin=Secret(valeur),
    )
    ct.write("chown", "root:git", cible)
    ct.write("chmod", "0640", cible)

    info(f"  {nom} = {valeur}")
    warn(f"  à ranger dans OpenBao MAINTENANT — {nom} ne sera pas réaffiché")
    if nom == "secret_key":
        warn("  sans secret_key, une base restaurée reste chiffrée et "
             "illisible : c'est le secret dont la perte coûte le plus cher")


class CompteAdmin(EtapeG):
    """Le premier compte, créé en ligne de commande.

    L'inscription publique est fermée et l'assistant web désarmé : il n'existe
    aucun autre chemin pour obtenir un premier administrateur, et c'est
    voulu — une instance neuve ne doit pas pouvoir être adoptée par le premier
    visiteur.

    `--must-change-password` : le mot de passe affiché ici est un mot de passe
    de transit. Il traverse un journal de terminal, il n'a pas vocation à
    rester.
    """

    name = "compte d'administration"
    requires = (SENTINELLE, "forgejo (armement)")

    def skip_if(self, ctx) -> str | None:
        if not ctx.opts.admin:
            return "sans --admin"
        return None

    def check(self, ctx) -> Outcome:
        nom = ctx.opts.admin
        res = self._ct(ctx).read(
            "sudo", "-u", "git", CT_BINAIRE, "--config", "/etc/forgejo/app.ini",
            "admin", "user", "list",
            check=False,
        )
        if not res.ok:
            return Outcome(
                "error",
                "impossible de lister les comptes — Forgejo est-il démarré ? "
                + (res.stderr.strip().splitlines() or [""])[0],
            )
        # La première colonne est l'identifiant ; l'en-tête est ignoré parce
        # qu'aucun compte ne s'appelle « ID ».
        comptes = {ligne.split()[1] for ligne in res.lines[1:] if ligne.split()}
        if nom in comptes:
            return Outcome(
                "ok",
                f"{nom} existe — inchangé ; pour le mot de passe, "
                "« forgejo admin user change-password »",
            )
        return Outcome(
            "absent",
            f"{nom} n'existe pas",
            (
                Action(
                    f"forgejo admin user create --admin --username {nom}",
                    lambda c, n=nom: _creer_admin(c, n),
                    generates_secret=True,
                ),
            ),
        )


def _creer_admin(ctx, nom: str) -> None:
    import base64
    import os

    # Alphanumérique : rien à échapper dans un terminal ni dans un
    # gestionnaire de mots de passe, et aucun risque qu'un caractère se perde
    # en chemin.
    brut = base64.b64encode(os.urandom(24)).decode()
    motdepasse = Secret("".join(c for c in brut if c.isalnum())[:24])

    ct = ctx.runner.for_container(ctx.opts.ctid)
    ct.write(
        "sudo", "-u", "git", CT_BINAIRE, "--config", "/etc/forgejo/app.ini",
        "admin", "user", "create",
        "--admin", "--username", nom,
        "--email", f"{nom}@lan.wittner.tech",
        "--password", motdepasse,
        "--must-change-password",
    )
    info(f"  {nom} / {motdepasse}")
    warn("  mot de passe de TRANSIT : il est à changer à la première connexion")


class PremiereSauvegarde(EtapeG):
    """Déclenchée seulement s'il n'existe aucune sauvegarde.

    Elle passe AVANT le hors-site dans l'ordre des étapes, et ce n'est pas
    cosmétique : sans elle, la première copie n'aurait rien à transférer.
    """

    name = "première sauvegarde"
    requires = (SENTINELLE, "fj (CT)", "forgejo (armement)")

    def check(self, ctx) -> Outcome:
        ct = self._ct(ctx)
        # Script CONSTANT, chemin en argument.
        compte = ct.read(
            "sh", "-c",
            "find \"$1\" -mindepth 1 -maxdepth 1 -type d -name '20*' "
            "! -name '*.part' 2>/dev/null | wc -l",
            "sh", ctx.opts.mp2_mount,
            check=False,
        ).out
        try:
            nombre = int(compte or 0)
        except ValueError:
            nombre = 0

        if nombre > 0:
            return Outcome("ok", f"{nombre} sauvegarde(s) présente(s)")
        if not ctx.opts.do_first_run:
            return Outcome(
                "error",
                "aucune sauvegarde et --no-first-run — "
                "la source de vérité reste sans filet",
            )
        return Outcome(
            "absent",
            "aucune sauvegarde — sans elle, rien à copier hors-site ni à restaurer",
            (
                Action(
                    "systemctl start fj-backup.service (CT)",
                    lambda c: Systemd(
                        c.runner.for_container(c.opts.ctid)
                    ).start("fj-backup.service"),
                ),
            ),
        )
