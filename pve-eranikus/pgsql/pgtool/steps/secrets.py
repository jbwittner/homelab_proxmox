"""Section G — première sauvegarde, compte d'administration, locataire.

DEUX DE CES ÉTAPES FONT APPARAÎTRE UN MOT DE PASSE. Elles ne sont donc jouées
ni par défaut, ni par surprise : chacune est derrière son drapeau, et l'action
qu'elles proposent est marquée `generates_secret`. Le moteur refuse alors de
l'exécuter sans autorisation explicite, et l'étape ressort « bloquée ».

**Un objet qui existe n'est jamais touché.** Rejouer un déploiement de routine
ne doit pas invalider un mot de passe déjà rangé dans OpenBao — il n'y a pas de
rotation par accident. Le remède, quand un mot de passe est perdu, est un
`ALTER ROLE` fait à la main depuis la porte `peer`.

**Le mot de passe passe par une variable psql, jamais par le texte SQL.** C'est
psql qui cite, donc le mot de passe peut contenir n'importe quel caractère — et
c'est aussi ce qui l'empêche de finir dans un journal d'erreur, grâce au type
`Secret`.
"""

from __future__ import annotations

import base64
import os

from core.commands import Psql, Systemd
from core.converge import Action, Outcome
from core.runner import Secret
from pgtool.deploy import MP


def _mot_de_passe() -> Secret:
    """Alphanumérique uniquement.

    Rien à échapper dans une URL de connexion ni dans un fichier de
    configuration : c'est ce qui évite de découvrir six mois plus tard qu'un
    caractère cassait la chaîne de connexion d'un service.
    """
    brut = base64.b64encode(os.urandom(32)).decode()
    return Secret("".join(c for c in brut if c.isalnum()))


class EtapeG:
    section = "G"
    requires: tuple[str, ...] = ()

    def skip_if(self, ctx) -> str | None:
        return None

    def _psql(self, ctx) -> Psql:
        return Psql(ctx.runner.for_container(ctx.opts.ctid))


class PremiereSauvegarde(EtapeG):
    """Déclenchée seulement s'il n'existe aucune sauvegarde.

    Elle passe AVANT le hors-site dans l'ordre des étapes, et ce n'est pas
    cosmétique : sans elle, la première copie n'aurait rien à transférer.
    """

    name = "première sauvegarde"

    def check(self, ctx) -> Outcome:
        ct = ctx.runner.for_container(ctx.opts.ctid)
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
                "le conteneur reste sans filet",
            )
        return Outcome(
            "absent",
            "aucune sauvegarde — sans elle, rien à copier hors-site ni à restaurer",
            (
                Action(
                    "systemctl start pg-backup.service (CT)",
                    lambda c: Systemd(
                        c.runner.for_container(c.opts.ctid)
                    ).start("pg-backup.service"),
                ),
            ),
        )


class RoleAdmin(EtapeG):
    """Le compte d'administration, créé une seule fois.

    Jamais de rotation : un rôle qui existe est laissé tel quel. Le mot de
    passe s'affiche une fois, à la création, et va dans OpenBao.
    """

    name = "compte d'administration"

    def skip_if(self, ctx) -> str | None:
        if not ctx.opts.admin:
            return "sans --admin"
        return None

    def check(self, ctx) -> Outcome:
        role = ctx.opts.admin
        if self._psql(ctx).role_exists(role):
            return Outcome(
                "ok",
                f"{role} existe — inchangé ; pour le mot de passe, "
                "ALTER ROLE depuis la porte peer",
            )
        return Outcome(
            "absent",
            f"{role} n'existe pas",
            (
                Action(
                    f'CREATE ROLE "{role}" LOGIN SUPERUSER',
                    lambda c, r=role: _creer_role(c, r),
                    generates_secret=True,
                ),
            ),
        )


def _creer_role(ctx, role: str) -> None:
    from core.log import info, warn

    psql = Psql(ctx.runner.for_container(ctx.opts.ctid))
    secret = _mot_de_passe()
    psql.run_sql(
        'CREATE ROLE :"role" LOGIN SUPERUSER PASSWORD :\'motdepasse\';',
        role=role,
        motdepasse=secret,
    )
    # Affiché UNE fois, ici, et nulle part ailleurs : le type Secret l'empêche
    # de ressortir dans une trace d'erreur.
    info(f"  {role} / {secret}")
    warn("  à ranger dans OpenBao maintenant — il ne sera pas réaffiché")
    warn("  puis ajouter la ligne hostssl correspondante dans pg_hba.conf")


class Locataire(EtapeG):
    """Une base et son rôle, isolés des autres.

    Joue `tenant.sql` DEPUIS LE MONTAGE : le fichier contient les `REVOKE` qui
    font la différence entre un cluster mutualisé et un cluster partagé par
    accident, et il utilise `\\connect`, donc il ne peut pas être découpé en
    ordres isolés.
    """

    name = "locataire"

    def skip_if(self, ctx) -> str | None:
        if not ctx.opts.tenant:
            return "sans --tenant"
        return None

    def check(self, ctx) -> Outcome:
        nom = ctx.opts.tenant
        # La BASE fait foi, pas le rôle : un rôle sans base n'est pas un
        # locataire, et c'est la base qui porte les ACL.
        if self._psql(ctx).database_exists(nom):
            return Outcome("ok", f"{nom} existe — inchangé")
        return Outcome(
            "absent",
            f"la base {nom} n'existe pas",
            (
                Action(
                    f"psql -f {MP}/tenant.sql -v name={nom}",
                    lambda c, n=nom: _creer_locataire(c, n),
                    generates_secret=True,
                ),
            ),
        )


def _creer_locataire(ctx, nom: str) -> None:
    from core.log import info, warn

    psql = Psql(ctx.runner.for_container(ctx.opts.ctid))
    secret = _mot_de_passe()
    psql.run_file(f"{MP}/tenant.sql", name=nom, password=secret)
    info(f"  {nom} / {secret}")
    warn("  à ranger dans OpenBao maintenant — il ne sera pas réaffiché")
    warn(f"  puis ajouter la ligne de {nom} dans pg_hba.conf, "
         "AVANT la règle reject, et rejouer pg deploy")
