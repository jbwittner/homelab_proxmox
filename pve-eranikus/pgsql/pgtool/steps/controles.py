"""Section C — les contrôles. On regarde, on dit, on ne touche à rien.

Aucune étape de cette section ne propose d'action. Ce n'est pas un oubli :
ces contrôles constatent des choses qu'on ne peut pas « poser » — un cluster
qui ne répond pas, une socket manquante, une règle `pg_hba` mal formée. Le
remède demande de regarder, pas d'appliquer.

TROIS PIÈGES DE PRODUCTION SONT ENCODÉS ICI.

**`SHOW listen_addresses` ment.** Il renvoie ce que la configuration demande,
pas ce que le processus a obtenu. Dans un conteneur, PostgreSQL peut démarrer
avant que l'interface ne porte son adresse, n'ouvrir que la socket locale, et
se déclarer `active (running)` malgré tout. Seul `ss -lntp` fait foi, et on en
attend **deux** sockets : `0.0.0.0` et `[::]`.

**Un `reload` réussi ne prouve pas qu'un fichier a été relu.** Ce qui fait foi
est `pg_hba_file_rules`, la vue de ce que le serveur a RÉELLEMENT chargé. Une
ligne mal formée y porte son erreur ; sans cette vue, elle est ignorée en
silence et l'accès qu'elle devait accorder n'existe simplement pas.

**Les deux timers ne vivent pas sur la même machine.** `pg-backup.timer` est
dans le conteneur, `pgbk-offsite.timer` sur le nœud. Les interroger au mauvais
endroit répond sur la mauvaise machine, et c'est la confusion la plus facile à
faire dans tout ce montage.
"""

from __future__ import annotations

from core.commands import Psql, Systemd
from core.converge import Outcome
from core.runner import CommandError

# Deux sockets attendues : IPv4 et IPv6. Le conteneur n'a qu'une interface,
# donc `listen_addresses = '*'` couvre exactement ces deux-là.
SOCKETS_ATTENDUES = 2


class Controle:
    """Socle : section C, aucune dépendance, jamais d'action."""

    section = "C"
    requires: tuple[str, ...] = ()

    def skip_if(self, ctx) -> str | None:
        return None


class HbaRules(Controle):
    """Ce que le serveur a réellement chargé, pas ce que le fichier contient."""

    name = "pg_hba"

    def check(self, ctx) -> Outcome:
        psql = Psql(ctx.runner.for_container(ctx.opts.ctid))
        try:
            regles = psql.hba_rules()
        except CommandError:
            return Outcome(
                "error",
                "pg_hba_file_rules illisible — PostgreSQL répond-il ?",
            )
        if not regles:
            return Outcome(
                "error",
                "aucune règle chargée — PostgreSQL répond-il ?",
            )
        # La dernière colonne porte l'erreur, vide quand la règle est bonne.
        fautives = [r for r in regles if len(r) > 6 and r[6].strip()]
        if fautives:
            lignes = ", ".join(r[0] for r in fautives)
            return Outcome(
                "error",
                f"{len(fautives)} règle(s) en erreur — lignes {lignes}",
            )
        return Outcome("ok", f"{len(regles)} règle(s), aucune erreur")


class SocketsEnEcoute(Controle):
    """Deux sockets sur 5432, ou le service n'écoute pas ce qu'il croit."""

    name = "listen 5432"

    def check(self, ctx) -> Outcome:
        res = ctx.runner.for_container(ctx.opts.ctid).read(
            "ss", "-lntp", check=False
        )
        sockets = [ligne for ligne in res.lines if ":5432" in ligne]
        if len(sockets) >= SOCKETS_ATTENDUES:
            return Outcome("ok", f"{len(sockets)} socket(s)")
        return Outcome(
            "error",
            f"{len(sockets)} socket(s) sur 5432, {SOCKETS_ATTENDUES} attendues — "
            "piège de listen_addresses en LXC, voir doc/RUNBOOK.md section 4",
        )


class TimerSauvegarde(Controle):
    """`pg-backup.timer`, DANS le conteneur.

    Le nom porte « état » : la section B a une étape qui ARME ce timer, et deux
    étapes homonymes rendraient une dépendance ambiguë et le bilan illisible.
    """

    name = "pg-backup.timer (état)"

    def check(self, ctx) -> Outcome:
        systemd = Systemd(ctx.runner.for_container(ctx.opts.ctid))
        if systemd.is_enabled("pg-backup.timer"):
            return Outcome("ok", "actif")
        return Outcome(
            "error",
            "inactif — le conteneur reste sans filet, aucune sauvegarde ne partira",
        )


class TimerHorsSite(Controle):
    """`pgbk-offsite.timer`, SUR LE NŒUD.

    Le bash n'avait pas de branche d'échec ici : un hors-site désarmé passait
    inaperçu au résumé, et personne ne s'apercevait que la copie ne partait
    plus. Une absence silencieuse de sauvegarde distante est exactement ce
    qu'on ne veut pas.
    """

    name = "pgbk-offsite.timer (état)"

    def skip_if(self, ctx) -> str | None:
        if not ctx.opts.do_offsite:
            return "--no-offsite"
        return None

    def check(self, ctx) -> Outcome:
        if Systemd(ctx.runner).is_enabled("pgbk-offsite.timer"):
            return Outcome("ok", "actif")
        return Outcome(
            "error",
            "inactif — aucune copie hors-site ne partira, "
            "vérifier la clé GCP et le volume mp2",
        )
