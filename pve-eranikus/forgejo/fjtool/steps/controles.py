"""Section C — les contrôles. On regarde, on dit, on ne touche à rien.

Aucune étape de cette section ne propose d'action. Ce n'est pas un oubli : ces
contrôles constatent des choses qu'on ne peut pas « poser » — un service qui
ne répond pas, une socket qui ne devrait pas exister, un journal qui se plaint.
Le remède demande de regarder, pas d'appliquer.

CETTE SECTION EST LA LISTE DE VÉRIFICATION DU SERVICE, RENDUE EXÉCUTABLE. Une
liste dans un document se lit une fois, à l'installation ; celle-ci repasse à
chaque `fj deploy --status`, c'est-à-dire les jours où quelque chose a bougé.

QUATRE PIÈGES SONT ENCODÉS ICI.

**`SHOW listen_addresses` ment.** Il renvoie ce que la configuration demande,
pas ce que le processus a obtenu. Ici on attend l'inverse du CT 200 : **zéro**
socket TCP sur 5432. Une socket qui apparaît veut dire que le drop-in n'a pas
été relu, et que la base est joignable depuis le LAN.

**Forgejo réécrit `app.ini` quand il lui manque un secret.** Le montage étant
en lecture seule, cette écriture échoue — et Forgejo continue quand même, en
laissant une ligne dans son journal. Sans contrôle, l'instance tourne avec des
secrets éphémères qui changent à chaque redémarrage, ce qui invalide toutes
les sessions et tous les jetons sans que rien ne le dise.

**Les ACL ne survivent pas à une migration de schéma mal comprise.** Le
`REVOKE CONNECT` est reposé en section P ; on le RELIT en fin de parcours,
après que Forgejo a créé ses tables — c'est le seul moment qui prouve qu'il
tient toujours.

**Les deux timers ne vivent pas sur la même machine.** `fj-backup.timer` est
dans le conteneur, `fjbk-offsite.timer` sur le nœud. Les interroger au mauvais
endroit répond sur la mauvaise machine, et c'est la confusion la plus facile à
faire dans tout ce montage.
"""

from __future__ import annotations

from core.commands import Systemd
from core.converge import Outcome
from fjtool import version as V
from fjtool.deploy import CT_APP_INI, CT_BINAIRE
from fjtool.steps.postgres import BASE, public_peut_se_connecter

# Une configuration qui porte encore un marqueur de gabarit n'a pas été
# renseignée. Le motif est volontairement improbable dans une vraie valeur.
MARQUEUR = "@@"


class Controle:
    """Socle : section C, aucune dépendance, jamais d'action."""

    section = "C"
    requires: tuple[str, ...] = ()

    def skip_if(self, ctx) -> str | None:
        return None

    def _ct(self, ctx):
        return ctx.runner.for_container(ctx.opts.ctid)


class AucuneSocketTcp(Controle):
    """PostgreSQL ne doit ouvrir AUCUNE socket TCP.

    C'est l'inverse du contrôle du CT 200, et c'est voulu : là-bas le cluster
    est mutualisé et doit être joignable ; ici il ne sert qu'un processus du
    même conteneur, donc `listen_addresses = ''`.

    Le contrôle porte sur `ss -lntp`, jamais sur `SHOW listen_addresses` : le
    second dit ce qui a été demandé, le premier ce qui a été obtenu. Les deux
    divergent exactement quand ça compte — quand le drop-in n'a pas été relu.
    """

    name = "aucune écoute TCP (5432)"

    def check(self, ctx) -> Outcome:
        res = self._ct(ctx).read("ss", "-lntp", check=False)
        sockets = [ligne for ligne in res.lines if ":5432" in ligne]
        if not sockets:
            return Outcome("ok", "aucune socket TCP — socket Unix uniquement")
        return Outcome(
            "error",
            f"{len(sockets)} socket(s) TCP sur 5432 alors que "
            f"listen_addresses doit être vide — la base est joignable depuis "
            f"le LAN ; le drop-in a-t-il été relu ? "
            f"(pct exec {ctx.opts.ctid} -- systemctl restart postgresql)",
        )


class AclApresMigration(Controle):
    """`REVOKE CONNECT … FROM PUBLIC`, relu APRÈS que le schéma existe.

    Le contrôle de la section P se fait avant que Forgejo n'ait migré son
    schéma. Celui-ci se fait après, et c'est le seul qui réponde à la question
    telle qu'elle se pose vraiment : « l'isolation est-elle encore là
    maintenant que l'application a travaillé ? »
    """

    name = "ACL (après initialisation)"

    def check(self, ctx) -> Outcome:
        from core.commands import Psql
        from core.runner import CommandError

        try:
            acl = Psql(self._ct(ctx)).database_acl(BASE)
        except CommandError:
            return Outcome("error", "base illisible — PostgreSQL répond-il ?")
        if not public_peut_se_connecter(acl):
            return Outcome("ok", acl)
        return Outcome(
            "error",
            f"PUBLIC peut se connecter à {BASE} (datacl : {acl or 'vide'}) — "
            "rejouer fj deploy pour rétablir les REVOKE",
        )


class InscriptionFermee(Controle):
    """L'inscription publique, et l'assistant d'installation.

    Lus dans la configuration RÉELLEMENT DÉPLOYÉE (`/etc/forgejo/app.ini`) et
    non dans celle du dépôt : c'est la copie du conteneur qui gouverne, et
    c'est elle qui peut avoir été réécrite.
    """

    name = "inscription publique fermée"

    ATTENDUS = {
        "INSTALL_LOCK": "true",
        "DISABLE_REGISTRATION": "true",
    }

    def check(self, ctx) -> Outcome:
        reglages = _lire_ini(self._ct(ctx), CT_APP_INI)
        if reglages is None:
            return Outcome("error", f"{CT_APP_INI} illisible")
        ecarts = [
            f"{cle} = {reglages.get(cle, 'absent')} (attendu {voulu})"
            for cle, voulu in self.ATTENDUS.items()
            if reglages.get(cle, "").lower() != voulu
        ]
        if ecarts:
            return Outcome(
                "error",
                "; ".join(ecarts)
                + " — une instance neuve pourrait être adoptée par le premier visiteur",
            )
        return Outcome("ok", "INSTALL_LOCK et DISABLE_REGISTRATION à true")


class ProxyDeConfiance(Controle):
    """`REVERSE_PROXY_TRUSTED_PROXIES` : une IP, jamais un joker.

    Avec `*`, n'importe quel client du LAN peut se déclarer n'importe quelle
    adresse par un en-tête `X-Forwarded-For` : les journaux d'audit et les
    limitations par IP ne veulent alors plus rien dire.

    Le marqueur de gabarit est traité comme une erreur à part entière, avec sa
    propre phrase : un `@@TRAEFIK_IP@@` resté en place est le symptôme d'une
    configuration jamais renseignée, pas d'une valeur trop large.
    """

    name = "proxy de confiance"

    def check(self, ctx) -> Outcome:
        reglages = _lire_ini(self._ct(ctx), CT_APP_INI)
        if reglages is None:
            return Outcome("error", f"{CT_APP_INI} illisible")
        valeur = reglages.get("REVERSE_PROXY_TRUSTED_PROXIES", "")

        if MARQUEUR in valeur:
            return Outcome(
                "error",
                f"« {valeur} » n'a jamais été renseigné — y mettre l'IP de "
                "Traefik dans ct/app.ini, puis rejouer fj deploy",
            )
        if not valeur:
            return Outcome("error", "vide — aucun proxy déclaré de confiance")
        if "*" in valeur:
            return Outcome(
                "error",
                f"« {valeur} » — un joker laisse n'importe quel client du LAN "
                "usurper une adresse par X-Forwarded-For",
            )
        return Outcome("ok", valeur)


class VersionEnService(Controle):
    """La version que le binaire DÉCLARE, comparée à l'épinglage.

    Doublon apparent de la section V, et c'est le but : celle-ci s'exécute en
    fin de parcours, donc après un éventuel redémarrage. Elle répond à « qu'est
    -ce qui tourne », là où la section V répond à « qu'est-ce qui est posé ».
    """

    name = "version servie"

    def check(self, ctx) -> Outcome:
        voulue = ctx.facts.get("version")
        if not voulue:
            return Outcome("error", "aucune version épinglée n'a été établie")
        res = self._ct(ctx).read(CT_BINAIRE, "--version", check=False)
        posee = V.version_installee(res.stdout) if res.ok else None
        if posee == voulue:
            return Outcome("ok", f"{posee} (branche {V.BRANCHE}, fin {V.EOL})")
        return Outcome(
            "error",
            f"{posee or 'binaire absent ou muet'} → attendu {voulue}",
        )


class JournalForgejo(Controle):
    """Ce que le service se plaint de ne pas pouvoir faire.

    Un seul motif est cherché, et c'est le piège nº 1 de ce montage : Forgejo
    qui n'arrive pas à écrire `app.ini`. Cela n'arrête pas le service — il
    continue avec des secrets tirés en mémoire, qui changent à chaque
    redémarrage. Sessions invalidées, jetons cassés, et aucune erreur visible
    ailleurs que dans ces quelques lignes.
    """

    name = "journal de forgejo"

    MOTIFS = ("Failed to save", "cannot write", "permission denied",
              "read-only file system")

    def check(self, ctx) -> Outcome:
        lignes = Systemd(self._ct(ctx)).journal("forgejo", lines=200)
        suspectes = [
            ligne for ligne in lignes
            if any(motif.lower() in ligne.lower() for motif in self.MOTIFS)
            and "app.ini" in ligne.lower()
        ]
        if not suspectes:
            return Outcome("ok", f"{len(lignes)} ligne(s), rien sur app.ini")
        return Outcome(
            "error",
            f"Forgejo n'arrive pas à écrire app.ini — il tourne alors avec des "
            f"secrets éphémères. Vérifier {CT_APP_INI} et les quatre fichiers "
            f"de /etc/forgejo/secrets : « {suspectes[0].strip()[:120]} »",
        )


class TimerSauvegarde(Controle):
    """`fj-backup.timer`, DANS le conteneur.

    Le nom porte « état » : la section B a une étape qui ARME ce timer, et deux
    étapes homonymes rendraient une dépendance ambiguë et le bilan illisible.
    """

    name = "fj-backup.timer (état)"

    def check(self, ctx) -> Outcome:
        if Systemd(self._ct(ctx)).is_enabled("fj-backup.timer"):
            return Outcome("ok", "actif")
        return Outcome(
            "error",
            "inactif — la source de vérité reste sans filet, "
            "aucune sauvegarde ne partira",
        )


class TimerHorsSite(Controle):
    """`fjbk-offsite.timer`, SUR LE NŒUD."""

    name = "fjbk-offsite.timer (état)"

    def skip_if(self, ctx) -> str | None:
        if not ctx.opts.do_offsite:
            return "--no-offsite"
        return None

    def check(self, ctx) -> Outcome:
        if Systemd(ctx.runner).is_enabled("fjbk-offsite.timer"):
            return Outcome("ok", "actif")
        return Outcome(
            "error",
            "inactif — aucune copie hors-site ne partira, "
            "vérifier la clé GCP et le volume mp2",
        )


# ─── lecture d'un fichier INI, côté conteneur ────────────────────────────────


def lire_ini(texte: str) -> dict[str, str]:
    """Aplatit un INI en « CLÉ → valeur », sections confondues.

    Volontairement à plat : les clés qui nous intéressent (`INSTALL_LOCK`,
    `DISABLE_REGISTRATION`, `REVERSE_PROXY_TRUSTED_PROXIES`) sont uniques dans
    tout le fichier, et suivre les sections n'apporterait que des occasions de
    se tromper de nom de section au fil des versions de Forgejo.

    `configparser` n'est pas utilisé : app.ini admet des sections à points
    (`[cron.update_checker]`) et des valeurs non citées qu'il gère, certes,
    mais il lèverait sur un fichier légèrement malformé — or c'est justement un
    fichier malformé qu'on veut pouvoir DIAGNOSTIQUER plutôt que subir.
    """
    reglages: dict[str, str] = {}
    for ligne in texte.splitlines():
        ligne = ligne.strip()
        if not ligne or ligne[0] in ";#[":
            continue
        cle, sep, valeur = ligne.partition("=")
        if sep:
            reglages[cle.strip()] = valeur.strip()
    return reglages


def _lire_ini(ct, chemin: str) -> dict[str, str] | None:
    res = ct.read("cat", chemin, check=False)
    if not res.ok:
        return None
    return lire_ini(res.stdout)
