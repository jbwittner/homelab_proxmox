"""`fj` — point d'entrée unique de l'outillage Forgejo.

**Un outil de NŒUD, et rien d'autre.** Il ne se pousse pas dans le conteneur
et n'y délègue aucune commande : tout passe par `pct exec`. Quatre verbes, et
ils se répartissent en deux familles :

    deploy, status      parlent au conteneur
    version, key        ne parlent qu'au dépôt et à internet

Les seconds se jouent donc très bien depuis un poste de développement, au
moment de commiter un épinglage.

Les sous-commandes restent importées **paresseusement**. Ce n'est plus pour
protéger un import dans le conteneur — il n'y en a plus — mais pour que
`fj version` ne charge ni `proxmox` ni le moteur de convergence pour lire un
fichier de quinze lignes.
"""

from __future__ import annotations

import argparse
import signal
import sys
from typing import Sequence

from core.log import error

# Le CTID du service, utilisé pour amorcer une installation vierge où
# /etc/default/fjbk n'existe pas encore — c'est `fj deploy` qui l'écrit.
# Tier 400–499 : installations manuelles à version épinglée (voir le README
# du dépôt). Forgejo prend le premier.
CTID_PAR_DEFAUT = 400


class Parser(argparse.ArgumentParser):
    """Un usage fautif sort en 1, pas en 2.

    `argparse` sort en 2 sur une erreur d'arguments, et 2 est un code qui veut
    dire autre chose dans ce homelab : « au moins un transfert a échoué », dans
    la table de `pg offsite`. Une faute de frappe consignée par systemd se
    lirait alors comme une panne de transfert, trois semaines plus tard. Une
    erreur d'usage appartient à la famille « environnement inutilisable »,
    soit 1 — c'est aussi ce que font les scripts bash de ce dépôt.

    `--help` continue de sortir en 0 : ce n'est pas une erreur.
    """

    def error(self, message: str):  # noqa: D102 - contrat d'argparse
        self.print_usage(sys.stderr)
        error(f"{self.prog} : {message}")
        raise SystemExit(1)


def _quitter_sur_signal(numero, _cadre):  # pragma: no cover - dépend du signal
    """130, pour SIGINT comme pour SIGTERM.

    Python ne rend 130 de lui-même que pour KeyboardInterrupt, et SIGTERM tue
    le processus par signal sans jamais produire de code. Les deux sont donc
    interceptés explicitement, sinon le contrat de codes de retour ne tient
    que pour la moitié des interruptions.
    """
    error(f"interrompu par signal ({signal.Signals(numero).name})")
    raise SystemExit(130)


# ─── fj version ──────────────────────────────────────────────────────────────


def _version(args: argparse.Namespace) -> int:
    """Lit l'épinglage, ou le résout depuis Codeberg.

    **Résoudre n'installe rien.** C'est toute la séparation qui manque au
    script communautaire : là-bas, « mettre à jour » veut dire « aller
    chercher la dernière et la poser », en un geste. Ici ce sont deux
    commandes, et celle qui pose ne parle à personne.
    """
    from core.log import info, step, warn
    from fjtool import version as V

    src = _source_du_depot(args)
    chemin = src / "ct" / "VERSION"

    actuelle = V.lire(chemin)
    if not args.resolve:
        if not actuelle:
            error(f"{chemin} ne porte aucune version")
            error("         la résoudre : fj version --resolve")
            return 1
        try:
            V.valider(actuelle)
        except V.VersionError as exc:
            error(str(exc))
            return 1
        info(f"{actuelle} — branche {V.BRANCHE} LTS, fin de support {V.EOL}")
        return 0

    step(f"résolution de la dernière {V.BRANCHE}.x stable depuis Codeberg")
    try:
        release = V.resoudre()
    except V.VersionError as exc:
        error(str(exc))
        return 1

    if release.tag == actuelle:
        info(f"{release.tag} — déjà épinglée, {chemin} inchangé")
        return 0

    chemin.write_text(V.rendre(release.tag), encoding="utf-8")
    info(f"{actuelle or 'non résolue'} → {release.tag}")
    info(f"  écrit dans {chemin}")
    warn("  rien n'est installé : « fj deploy » pose ce que ce fichier dit")
    warn("  commiter ce changement — l'épinglage n'a de valeur que tracé")
    return 0


# ─── fj key ──────────────────────────────────────────────────────────────────


def _key(args: argparse.Namespace) -> int:
    """Lit l'empreinte épinglée, ou récupère la clé et l'épingle.

    Même séparation que pour la version, et pour la même raison : **récupérer
    et poser sont deux gestes**. `fj deploy` n'interroge personne — il vérifie
    que la clé du dépôt correspond toujours à l'empreinte du dépôt.
    """
    from core.log import info, step, warn
    from core.runner import Runner
    from fjtool import cle as K
    from fjtool.deploy import Paths

    paths = Paths(src=_source_du_depot(args))
    runner = Runner()

    epinglee = K.lire(paths.key_fingerprint)

    if not args.fetch:
        if not epinglee:
            error(f"{paths.key_fingerprint} ne porte aucune empreinte")
            error("         l'épingler : fj key --fetch")
            return 1
        info(f"{epinglee}")
        if not paths.release_key.is_file():
            warn(f"  {paths.release_key} absent — rejouer fj key --fetch")
            return 1
        # Ce que le dépôt porte VRAIMENT, et non ce qu'il prétend porter : les
        # deux fichiers peuvent diverger si l'un a été édité à la main.
        try:
            trouvees = K.empreintes(runner, paths.release_key)
            K.retenir(trouvees, epinglee=epinglee)
        except K.CleError as exc:
            error(str(exc))
            return 1
        info(f"  {paths.release_key} correspond")
        return 0

    source = args.source or K.URL_PAR_DEFAUT
    step(f"récupération de la clé depuis {source}")
    try:
        bloc = K.recuperer(source)
        paths.release_key.write_bytes(bloc)
        trouvees = K.empreintes(runner, paths.release_key)
        retenue = K.retenir(trouvees, epinglee=epinglee)
    except K.CleError as exc:
        error(str(exc))
        return 1

    if epinglee == retenue:
        info(f"  {retenue} — déjà épinglée, inchangée")
        return 0

    paths.key_fingerprint.write_text(
        K.rendre(retenue, source=source), encoding="utf-8"
    )
    info(f"  empreinte : {retenue}")
    info(f"  écrite dans {paths.key_fingerprint}")
    warn("  À COMMITER — c'est ce fichier qui rend un changement de clé visible")
    warn("  Facultatif, une minute : comparer cette empreinte à celle annoncée")
    warn("  par le projet ailleurs que sur la page de téléchargement.")
    return 0


# ─── fj deploy ───────────────────────────────────────────────────────────────


def racine_du_service(candidats):
    """Le premier candidat qui porte `ct/app.ini`, sinon un refus argumenté.

    Fonction PURE : toute la décision est ici, et se teste sans toucher au
    système de fichiers de production.

    LE REFUS NOMME TOUT CE QUI A ÉTÉ ESSAYÉ, et c'est le fruit d'un défaut
    réel. Il n'en citait qu'un — celui de la copie installée — ce qui donnait,
    à un opérateur pourtant placé dans le bon répertoire :

        /usr/local/lib/fjtool ne ressemble pas au service Forgejo du dépôt
        — jouer fj depuis le dépôt

    Il y était. Un message qui envoie là où l'on se trouve déjà est pire qu'un
    message absent : on cherche ce qu'on a mal fait, et il n'y a rien.
    """
    from fjtool.location import Refus

    candidats = list(candidats)
    for candidat in candidats:
        if (candidat / "ct" / "app.ini").is_file():
            return candidat

    essayes = "\n".join(f"         — {c}" for c in candidats)
    raise Refus(
        "aucun service Forgejo trouvé (ct/app.ini introuvable).\n"
        f"         Essayé :\n{essayes}\n"
        "         Se placer dans le répertoire du service, jouer « ./fj » "
        "depuis le dépôt,\n"
        "         ou préciser « --src <chemin du service> »."
    )


def _source_du_depot(args: argparse.Namespace, *, module: "Path | None" = None):
    """La racine du service DANS LE DÉPÔT, jamais la copie installée.

    Poser depuis `/usr/local/lib/fjtool` reviendrait à redéployer ce qui est
    déjà là : le déploiement n'aurait plus de source de vérité, et un
    `git pull` cesserait d'avoir le moindre effet.

    DEUX CANDIDATS, dans cet ordre :

      1. le répertoire du module — c'est le cas de `./fj` joué depuis le
         dépôt, la forme que la documentation écrit partout ;
      2. **le répertoire courant** — c'est le cas de `fj` tapé sans chemin,
         qui résout par le PATH vers la copie installée. L'opérateur est alors
         presque toujours dans le répertoire du service, et l'y chercher est
         la seule chose raisonnable à faire.

    `module` n'existe que pour les tests : il remplace `__file__`.
    """
    from pathlib import Path

    if getattr(args, "src", None):
        return racine_du_service([Path(args.src).resolve()])

    # `fjtool/cli.py` → `fjtool/` → la racine du service.
    depuis_module = Path(module or __file__).resolve().parents[1]
    return racine_du_service([depuis_module, Path.cwd().resolve()])


def _contexte_deploy(args: argparse.Namespace, *, ctid: int, runner, src):
    from core.converge import Mode
    from fjtool.deploy import Options, Paths, contexte

    mode = Mode.APPLY
    if args.status:
        mode = Mode.STATUS
    elif args.dry_run:
        mode = Mode.DRY_RUN

    return contexte(
        runner=runner,
        paths=Paths(src=src),
        opts=Options(
            ctid=ctid,
            do_container=not args.no_container,
            do_install=not args.no_install,
            admin=args.admin,
        ),
        mode=mode,
        # Deux drapeaux, deux natures de secret : `--secrets` pose les clés de
        # chiffrement de l'instance, `--admin` crée un compte. Les confondre
        # ferait qu'un simple ajout de compte pourrait régénérer SECRET_KEY —
        # et rendre illisible tout ce que la base contient déjà.
        allow_secrets=args.secrets or bool(args.admin),
    )


def _code_de_sortie(rapports) -> int:
    from core.converge import BLOCKED, SKIP

    for rapport in rapports:
        if rapport.state in (SKIP, BLOCKED, "ok", "drift", "absent"):
            continue
        return 1
    return 0


def _deploy(args: argparse.Namespace) -> int:
    """Un parcours, trois modes, un bilan."""
    import os

    from core.converge import Mode, render_report, render_summary
    from core.log import info, step
    from core.runner import Runner
    from fjtool.location import Refus, read_conf, resolve_ctid
    from fjtool.plan import deployer

    if os.geteuid() != 0:
        raise Refus("à lancer en root sur le nœud (pct l'exige)")

    src = _source_du_depot(args)
    ctid = resolve_ctid(
        flag=args.ctid, env=os.environ, conf=read_conf(),
        defaut=CTID_PAR_DEFAUT,
    )
    ctx = _contexte_deploy(args, ctid=ctid, runner=Runner(), src=src)

    # `host/` a disparu avec le hors-site : ne l'annoncer plus serait mentir
    # sur ce que le déploiement va lire.
    step(f"CT {ctid} — dépôt {src} (mp1 : ct/, tout le reste depuis le nœud)")
    if ctx.mode is not Mode.APPLY:
        info(f"(mode --{ctx.mode.value} : aucune modification)")

    rapports = deployer(ctx)

    step("Résumé")
    rendu = render_report(rapports) if ctx.mode is Mode.STATUS \
        else render_summary(rapports)
    for ligne in rendu.splitlines():
        info(ligne)
    if ctx.mode is not Mode.APPLY:
        info("Aucune modification appliquée.")
    return _code_de_sortie(rapports)


# ─── fj status ───────────────────────────────────────────────────────────────


def _status(args: argparse.Namespace) -> int:
    """Les maillons du montage, regardés ENSEMBLE."""
    import os
    from types import SimpleNamespace

    from core.log import detail, error as journal_erreur, info, step
    from core.runner import Runner
    from fjtool.deploy import Options, Paths
    from fjtool.etat import alarmes, code_de_sortie, relever, render_etat
    from fjtool.location import Refus, read_conf, resolve_ctid

    if os.geteuid() != 0:
        raise Refus("à lancer en root sur le nœud (pct l'exige)")

    ctid = resolve_ctid(flag=args.ctid, env=os.environ, conf=read_conf())
    # `paths` sert au seul contrôle qui lise le dépôt : la version épinglée,
    # que l'on compare à celle réellement servie.
    ctx = SimpleNamespace(
        runner=Runner(),
        opts=Options(ctid=ctid),
        paths=Paths(src=_source_du_depot(args)),
    )
    etat = relever(ctx)

    step(f"CT {ctid} — état du montage")
    # Le tableau est une DONNÉE : il se recopie tel quel, sans horodatage. Les
    # alarmes sont des messages sur cette donnée, elles passent donc par la
    # journalisation.
    detail(render_etat(etat))
    for maillon in alarmes(etat):
        journal_erreur(f"{maillon.nom} : {maillon.detail}")
    if not alarmes(etat):
        info("les trois maillons répondent")
    return code_de_sortie(etat)


# ─── le parseur ──────────────────────────────────────────────────────────────


def construire_parseur() -> Parser:
    parseur = Parser(prog="fj", description="Outillage du CT Forgejo (LTS épinglée)")
    # Le CTID est accepté AVANT comme APRÈS la sous-commande. Ne le poser que
    # sur le parseur global rendrait `fj deploy --ctid 400` invalide — la forme
    # écrite partout dans la documentation. Défaut constaté sur `pg` le
    # 21 août 2026, et il n'avait été trouvé qu'en confrontant les documents
    # au parseur réel.
    parseur.add_argument("--ctid", help="conteneur cible (défaut : /etc/default/fjbk)")
    sous = parseur.add_subparsers(dest="commande", required=True)

    def ctid_local(p):
        # `SUPPRESS` et non `None`, et c'est tout le sujet : les deux options
        # écrivent dans le MÊME attribut du namespace. Avec un défaut ordinaire,
        # la sous-commande — analysée en second — réécrit `ctid=None` par-dessus
        # la valeur du parseur global, et `fj --ctid 500 deploy` retombe
        # silencieusement sur /etc/default/fjbk. La commande réussit ; elle vise
        # simplement un autre conteneur que celui demandé.
        #
        # Avec `SUPPRESS`, l'attribut n'est pas posé quand l'option est absente,
        # donc la valeur globale survit. Vu rouge avant d'être corrigé.
        p.add_argument(
            "--ctid",
            default=argparse.SUPPRESS,
            help="conteneur cible, pour cette commande",
        )

    # -- deploy ----------------------------------------------------------
    dep = sous.add_parser("deploy", help="pose ou met à jour tout le montage")
    ctid_local(dep)
    dep.add_argument("--src", help="racine du service dans le dépôt")
    dep.add_argument("--status", action="store_true",
                     help="verdicts seuls, ne change rien")
    dep.add_argument("--dry-run", action="store_true",
                     help="annonce ce qui serait fait, effets compris")
    dep.add_argument("--no-container", action="store_true",
                     help="ne touche pas au CT")
    dep.add_argument("--no-install", action="store_true",
                     help="n'installe aucun paquet ni binaire")
    dep.add_argument("--secrets", action="store_true",
                     help="autorise la génération des secrets manquants")
    dep.add_argument("--admin", metavar="NOM",
                     help="crée un compte d'administration (affiché une fois)")
    dep.set_defaults(fonction=_deploy)

    # -- version ---------------------------------------------------------
    ver = sous.add_parser("version", help="l'épinglage : le lire, ou le résoudre")
    ctid_local(ver)
    ver.add_argument("--src", help="racine du service dans le dépôt")
    ver.add_argument("--resolve", action="store_true",
                     help="interroge Codeberg et réécrit ct/VERSION")
    ver.set_defaults(fonction=_version)

    # -- key -------------------------------------------------------------
    key = sous.add_parser(
        "key", help="la clé de signature : l'empreinte épinglée, ou la récupérer"
    )
    ctid_local(key)
    key.add_argument("--src", help="racine du service dans le dépôt")
    key.add_argument("--fetch", action="store_true",
                     help="récupère la clé et épingle son empreinte")
    key.add_argument("--from", dest="source", metavar="URL|FICHIER",
                     help="d'où récupérer la clé (défaut : le site du projet)")
    key.set_defaults(fonction=_key)

    # -- status ----------------------------------------------------------
    sta = sous.add_parser("status", help="les maillons du montage, ensemble")
    ctid_local(sta)
    sta.add_argument("--src", help="racine du service dans le dépôt")
    sta.set_defaults(fonction=_status)

    return parseur


def main(argv: Sequence[str] | None = None) -> int:
    from fjtool.location import Refus

    signal.signal(signal.SIGINT, _quitter_sur_signal)
    signal.signal(signal.SIGTERM, _quitter_sur_signal)

    parseur = construire_parseur()
    args = parseur.parse_args(argv)

    try:
        return args.fonction(args)
    except Refus as refus:
        # Un refus vide veut dire « le message est déjà passé » : le moteur du
        # conteneur formate lui-même ses lignes, les repasser par error() les
        # préfixerait d'un second horodatage.
        if str(refus):
            error(str(refus))
        return 1
    except KeyboardInterrupt:  # pragma: no cover - dépend du signal
        error("interrompu")
        return 130
