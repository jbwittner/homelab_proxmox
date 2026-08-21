"""Section V — l'installation binaire épinglée, et sa vérification.

C'est la section qui existe parce que le script communautaire ne convient pas.
Elle tient en une phrase : **on pose exactement ce que `ct/VERSION` dit, et
seulement après l'avoir vérifié.**

LE TÉLÉCHARGEMENT SE FAIT SUR LE NŒUD, PAS DANS LE CONTENEUR. Trois raisons,
et la troisième suffit à elle seule :

  - le conteneur est la source de vérité d'ArgoCD ; lui donner un accès
    sortant en plus de son accès entrant élargit sa surface pour rien ;
  - la vérification GPG demande un trousseau et `gpg`, qui n'ont aucune raison
    d'exister dans le conteneur ;
  - **ce qui n'a pas été vérifié ne doit jamais toucher le disque du
    conteneur.** Télécharger dedans puis vérifier dedans, c'est déjà avoir
    écrit l'artefact non vérifié à l'endroit où il sera exécuté.

LA SOMME DE CONTRÔLE NE PROUVE RIEN TOUTE SEULE : elle voyage sur le même
canal que le binaire, donc qui peut remplacer l'un peut remplacer l'autre.
C'est la SIGNATURE qui rattache l'artefact à une clé — et cette clé doit avoir
été obtenue autrement que par le canal qu'elle sert à valider. D'où
`ct/RELEASE-KEY.asc`, déposé à la main une fois, hors de ce déploiement.
La somme reste vérifiée quand même : elle attrape les téléchargements
tronqués, qui sont bien plus fréquents qu'une attaque.
"""

from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

from core.converge import Action, Outcome
from core.log import info
from fjtool import version as V
from fjtool.deploy import CT_BINAIRE, CT_SYMLINK
from fjtool.steps.conteneur import EFFET_FORGEJO_RESTART, pousser

# Où le nœud dépose ce qu'il télécharge. Hors de /tmp : un artefact de 100 Mo
# qui disparaît au redémarrage se retéléchargerait à chaque déploiement, et on
# veut au contraire pouvoir reposer le même octet-pour-octet.
CACHE = Path("/var/cache/fjtool")

# Le trousseau dédié. JAMAIS le trousseau par défaut de root : y importer une
# clé de publication la rendrait de confiance pour tout ce que root vérifie
# ensuite, bien au-delà de Forgejo.
TROUSSEAU = Path("/var/lib/fjtool/forgejo-release.gpg")

VERSION_EPINGLEE = "version épinglée"
CLE_DE_PUBLICATION = "clé de publication"


class EtapeV:
    section = "V"
    requires: tuple[str, ...] = (VERSION_EPINGLEE,)

    def skip_if(self, ctx) -> str | None:
        return None


class VersionEpinglee:
    """Lit `ct/VERSION`, et refuse tout ce qui n'est pas la branche LTS.

    **Non résolue n'est pas « la dernière ».** C'est toute la différence avec
    `fetch_and_deploy_codeberg_release … "latest"`, et c'est pourquoi cette
    étape est en `error` plutôt qu'en `absent` : il n'existe aucune action qui
    puisse la corriger toute seule. Résoudre une version est une décision, pas
    une convergence — d'où une commande séparée, `fj version --resolve`.
    """

    name = VERSION_EPINGLEE
    section = "V"
    requires: tuple[str, ...] = ()

    def skip_if(self, ctx) -> str | None:
        return None

    def check(self, ctx) -> Outcome:
        chemin = ctx.paths.version_file
        brut = V.lire(chemin)
        if not brut:
            return Outcome(
                "error",
                f"{chemin} ne porte aucune version — "
                "la résoudre : fj version --resolve (n'installe rien)",
            )
        try:
            version = V.valider(brut)
        except V.VersionError as exc:
            return Outcome("error", str(exc))
        ctx.facts["version"] = version
        ctx.facts["release"] = V.Release(version)
        return Outcome("ok", f"{version} — branche {V.BRANCHE} LTS, fin {V.EOL}")


class CleDePublication:
    """La clé qui signe les publications Forgejo, dans un trousseau dédié.

    Elle n'est PAS téléchargée par ce code, et ce n'est pas une lacune : une
    clé récupérée par le même canal que l'artefact ne prouve rien de plus que
    l'artefact. Elle est déposée à la main dans `ct/RELEASE-KEY.asc` après
    avoir été confrontée à une source indépendante — voir doc/RUNBOOK.md § 4.

    Ce que fait cette étape : constater qu'elle est là, et l'importer dans un
    trousseau à part.
    """

    name = CLE_DE_PUBLICATION
    section = "V"
    # `gpg` est un outil du NŒUD : c'est lui qui télécharge et vérifie.
    requires: tuple[str, ...] = ("gnupg",)

    def skip_if(self, ctx) -> str | None:
        return None

    def check(self, ctx) -> Outcome:
        source = ctx.paths.ct_src / "RELEASE-KEY.asc"
        if not source.is_file():
            return Outcome(
                "error",
                f"{source} absent — sans clé, rien ne peut être vérifié et "
                "rien ne sera installé ; voir doc/RUNBOOK.md section 4",
            )
        empreintes = _empreintes_du_trousseau(ctx)
        if empreintes:
            ctx.facts["gpg_ok"] = True
            return Outcome("ok", f"{TROUSSEAU} — {', '.join(empreintes)}")
        return Outcome(
            "absent",
            f"{TROUSSEAU} vide ou absent",
            (
                Action(
                    f"gpg --import {source} → {TROUSSEAU}",
                    lambda c, s=source: _importer(c, s),
                ),
            ),
        )


def _empreintes_du_trousseau(ctx) -> list[str]:
    if not TROUSSEAU.is_file():
        return []
    res = ctx.runner.read(
        "gpg", "--no-default-keyring", "--keyring", str(TROUSSEAU),
        "--list-keys", "--with-colons",
        check=False,
    )
    return [
        ligne.split(":")[9]
        for ligne in res.lines
        if ligne.startswith("fpr:")
    ]


def _importer(ctx, source: Path) -> None:
    TROUSSEAU.parent.mkdir(parents=True, exist_ok=True)
    ctx.runner.write(
        "gpg", "--no-default-keyring", "--keyring", str(TROUSSEAU),
        "--batch", "--import", str(source),
    )


class BinaireForgejo(EtapeV):
    """Le binaire du conteneur, comparé à l'épinglage.

    La comparaison porte sur la VERSION QUE LE BINAIRE DÉCLARE, pas sur une
    empreinte : Codeberg republie des artefacts identiques sous des tags
    différents, et une empreinte enregistrée ici deviendrait un second
    épinglage, désynchronisé du premier.
    """

    name = "binaire Forgejo"
    requires = (VERSION_EPINGLEE, CLE_DE_PUBLICATION, "/opt/forgejo")

    def check(self, ctx) -> Outcome:
        voulue = ctx.facts["version"]
        release: V.Release = ctx.facts["release"]
        ct = ctx.runner.for_container(ctx.opts.ctid)

        res = ct.read(CT_BINAIRE, "--version", check=False)
        posee = V.version_installee(res.stdout) if res.ok else None

        if posee == voulue:
            return Outcome("ok", f"{CT_BINAIRE} — {posee}")

        if not ctx.opts.do_install:
            return Outcome(
                "error",
                f"{posee or 'aucun binaire'} → attendu {voulue}, et --no-install",
            )

        etat = "drift" if posee else "absent"
        return Outcome(
            etat,
            f"{posee or 'absent'} → {voulue}",
            (
                Action(
                    f"télécharger et vérifier {release.binaire} (nœud)",
                    lambda c, r=release: _obtenir_verifie(c, r),
                ),
                Action(
                    f"pct push {ctx.opts.ctid} → {CT_BINAIRE} (0755)",
                    lambda c, r=release: pousser(
                        c, CACHE / r.binaire, CT_BINAIRE, "0755"),
                    effects=frozenset({EFFET_FORGEJO_RESTART}),
                ),
            ),
        )


# ─── obtention et vérification, sur le nœud ──────────────────────────────────


def telecharger(url: str, cible: Path, *, timeout: int = 120) -> None:
    """Écrit `url` dans `cible`, par un fichier temporaire renommé.

    Le renommage final est ce qui garantit qu'un fichier présent dans le cache
    est un fichier COMPLET : une coupure réseau laisse un `.part`, jamais un
    artefact tronqué qui passerait pour bon au déploiement suivant.
    """
    cible.parent.mkdir(parents=True, exist_ok=True)
    partiel = cible.with_suffix(cible.suffix + ".part")
    with urllib.request.urlopen(url, timeout=timeout) as reponse:
        partiel.write_bytes(reponse.read())
    partiel.replace(cible)


def sha256(chemin: Path) -> str:
    empreinte = hashlib.sha256()
    with chemin.open("rb") as flux:
        for bloc in iter(lambda: flux.read(1024 * 1024), b""):
            empreinte.update(bloc)
    return empreinte.hexdigest()


def somme_attendue(texte: str) -> str:
    """Extrait l'empreinte d'un fichier `.sha256`.

    Le format est « <hex>  <nom> ». On ne prend que le premier champ : le nom
    varie d'une publication à l'autre, l'empreinte non.
    """
    premier = texte.split()
    if not premier:
        raise V.VersionError("fichier .sha256 vide")
    return premier[0]


def _obtenir_verifie(ctx, release: V.Release) -> None:
    """Télécharge les trois fichiers, vérifie, et ne garde que si tout passe.

    L'ORDRE COMPTE. La somme d'abord — elle est locale et instantanée, elle
    élimine le cas fréquent (téléchargement tronqué) sans déranger gpg. La
    signature ensuite, qui est la seule qui prouve quelque chose.

    En cas d'échec, l'artefact est SUPPRIMÉ du cache. Le laisser en place
    ferait qu'un second passage le trouverait « déjà là ».
    """
    binaire = CACHE / release.binaire
    asc = CACHE / f"{release.binaire}.asc"
    somme = CACHE / f"{release.binaire}.sha256"

    info(f"  téléchargement de {release.url()}")
    telecharger(release.url(), binaire)
    telecharger(release.url(".asc"), asc)
    telecharger(release.url(".sha256"), somme)

    try:
        attendue = somme_attendue(somme.read_text(encoding="utf-8"))
        obtenue = sha256(binaire)
        if obtenue != attendue:
            raise V.VersionError(
                f"somme de contrôle divergente pour {release.binaire}\n"
                f"         attendue : {attendue}\n"
                f"         obtenue  : {obtenue}"
            )
        info(f"  sha256 conforme : {obtenue}")

        res = ctx.runner.read(
            "gpg", "--no-default-keyring", "--keyring", str(TROUSSEAU),
            "--batch", "--verify", str(asc), str(binaire),
            check=False,
        )
        if not res.ok:
            raise V.VersionError(
                f"signature GPG NON vérifiée pour {release.binaire} — "
                "rien ne sera installé\n"
                + (res.stderr.strip() or res.stdout.strip())
            )
        info("  signature GPG vérifiée")
    except Exception:
        # Ne jamais laisser dans le cache un artefact dont la vérification a
        # échoué : le prochain passage le prendrait pour acquis.
        for fichier in (binaire, asc, somme):
            fichier.unlink(missing_ok=True)
        raise


class SymlinkForgejo(EtapeV):
    """`/usr/local/bin/forgejo` → `/opt/forgejo/forgejo`.

    Confort humain uniquement : l'unité systemd lance le chemin absolu, et
    doit pouvoir démarrer même si ce lien manque.
    """

    name = "forgejo (symlink)"
    requires = (VERSION_EPINGLEE, "binaire Forgejo")

    def check(self, ctx) -> Outcome:
        ct = ctx.runner.for_container(ctx.opts.ctid)
        vu = ct.read("readlink", "-f", CT_SYMLINK, check=False).out
        if vu == CT_BINAIRE:
            return Outcome("ok", f"{CT_SYMLINK} → {CT_BINAIRE}")
        return Outcome(
            "drift" if vu else "absent",
            f"{CT_SYMLINK} → {vu or 'rien'}",
            (
                Action(
                    f"ln -sfn {CT_BINAIRE} {CT_SYMLINK} (CT)",
                    lambda c: c.runner.for_container(c.opts.ctid).write(
                        "ln", "-sfn", CT_BINAIRE, CT_SYMLINK),
                ),
            ),
        )


class DurcissementGit(EtapeV):
    """`fsckObjects` sur les trois chemins d'entrée d'objets git.

    Équivalent manuel d'un durcissement arrivé en v16 : on ne l'attend pas
    deux ans. Ce que chacun couvre, et pourquoi les trois :

      transfer.fsckObjects  le réglage parapluie ;
      receive.fsckObjects   ce qui ENTRE par un push — le chemin par lequel un
                            utilisateur du homelab peut écrire ;
      fetch.fsckObjects     ce qui entre par un miroir tiré depuis l'extérieur.

    Un objet incohérent accepté aujourd'hui est un dépôt qu'on ne peut plus
    cloner demain. Pour une source de vérité, c'est la panne qui coûte le plus
    cher, parce qu'elle ne se voit qu'au moment de s'en servir.

    `--system`, donc `/etc/gitconfig` : le réglage vaut pour l'utilisateur
    `git` comme pour tout autre, et survit à une recréation du home.
    """

    name = "durcissement git"
    requires = (VERSION_EPINGLEE, "git (CT)")

    REGLAGES = ("transfer.fsckObjects", "receive.fsckObjects",
                "fetch.fsckObjects")

    def check(self, ctx) -> Outcome:
        ct = ctx.runner.for_container(ctx.opts.ctid)
        manquants = [
            nom for nom in self.REGLAGES
            if ct.read("git", "config", "--system", "--get", nom,
                       check=False).out != "true"
        ]
        if not manquants:
            return Outcome("ok", " ".join(self.REGLAGES))
        return Outcome(
            "drift" if len(manquants) < len(self.REGLAGES) else "absent",
            "non posés : " + ", ".join(manquants),
            tuple(
                Action(
                    f"git config --system {nom} true (CT)",
                    lambda c, n=nom: c.runner.for_container(
                        c.opts.ctid).write(
                        "git", "config", "--system", n, "true"),
                )
                for nom in manquants
            ),
        )
