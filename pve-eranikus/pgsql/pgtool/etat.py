"""`pg status` — les trois maillons du montage, regardés ensemble.

Le montage a trois maillons dont chacun peut se rompre en silence : la
sauvegarde locale, le timer qui la déclenche, la copie hors-site. Rien ne les
regardait ensemble — `pg-deploy.sh --status` dit si les FICHIERS sont en place,
ce qui est une autre question. Or les pannes qui coûtent cher ici sont
silencieuses par nature : un timer armé qui échoue chaque nuit reste armé, et
une sauvegarde qui ne part plus ne se découvre qu'au moment où l'on aurait eu
besoin de restaurer.

LE CONSTAT EST SÉPARÉ DU RENDU. `relever()` parle à l'infrastructure ;
`alarmes()` et `render_etat()` sont des fonctions pures de ce qu'il en a
rapporté. C'est ce qui rend les alarmes testables sans conteneur — et une
alarme qu'on ne peut pas tester ne vaut rien.

« NON DÉTERMINÉ » N'EST PAS « VA BIEN ». Un âge inconnu, un bucket qui n'a pas
répondu : ce sont des alarmes, pas des silences. C'est la même règle que celle
qui empêche le hors-site de s'armer sur un `mp2` non constaté.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# La sauvegarde tourne à 2h30 avec 15 minutes d'aléa. Passé 26 heures, une
# exécution a forcément été manquée — la marge couvre l'aléa et un décalage de
# démarrage, sans laisser passer un jour entier.
AGE_ALARME_H = 26

INCONNU = "?"


@dataclass(frozen=True)
class UniteEtat:
    """Un timer, vu là où il vit.

    `arme` et `actif` valent `None` quand la machine n'a pas répondu : ne pas
    savoir n'est pas la même chose qu'être désarmé, et les deux appellent des
    gestes différents.
    """

    nom: str
    arme: bool | None
    actif: bool | None
    prochain: str = ""
    dernier_resultat: str = ""

    @property
    def en_panne(self) -> bool:
        """Armé, et pourtant en échec à sa dernière exécution.

        « actif » ne dit rien du résultat : c'est la panne la plus discrète du
        montage, et celle qu'aucun `is-enabled` ne révèle.
        """
        return bool(self.arme) and self.dernier_resultat not in ("", "success")


@dataclass(frozen=True)
class Etat:
    """Ce qu'on a pu constater. `None` = non déterminé, jamais « faux »."""

    ctid: int
    ct_actif: bool | None
    sauvegardes: int
    dernier: str
    age_heures: int | None
    taille: str
    libre_mo: int | None
    timer_sauvegarde: UniteEtat
    timer_horssite: UniteEtat
    # Nombre d'instantanés vus dans le bucket, et ceux qui n'y sont pas.
    distants: int | None
    manquants: tuple[str, ...]


# ─── les alarmes ─────────────────────────────────────────────────────────────


def alarmes(etat: Etat) -> list[str]:
    """Ce qui ne va pas, du plus grave au moins grave.

    L'ordre n'est pas cosmétique : la première ligne est celle qu'on lit quand
    on n'en lit qu'une.
    """
    dits: list[str] = []

    if etat.sauvegardes == 0:
        dits.append(
            f"AUCUNE sauvegarde dans le CT {etat.ctid} — le cluster est sans "
            "filet ; lancer « pg backup »"
        )
    elif etat.age_heures is None:
        dits.append(
            "âge de la dernière sauvegarde non déterminé — ne pas savoir "
            "n'est pas aller bien"
        )
    elif etat.age_heures > AGE_ALARME_H:
        dits.append(
            f"dernière sauvegarde il y a {etat.age_heures} h "
            f"(seuil {AGE_ALARME_H} h) — une exécution a été manquée"
        )

    if etat.ct_actif is False:
        dits.append(
            f"CT {etat.ctid} à l'arrêt — aucune base n'est servie, aucune "
            "sauvegarde ne partira"
        )
    elif etat.ct_actif is None:
        dits.append(f"état du CT {etat.ctid} non déterminé")

    for unite, ou in ((etat.timer_sauvegarde, f"CT {etat.ctid}"),
                      (etat.timer_horssite, "nœud")):
        if unite.arme is None:
            dits.append(f"{unite.nom} ({ou}) : état non déterminé")
        elif not unite.arme:
            dits.append(f"{unite.nom} ({ou}) désarmé — rien ne se déclenchera")
        elif unite.en_panne:
            dits.append(
                f"{unite.nom} ({ou}) armé mais sa dernière exécution a échoué "
                f"({unite.dernier_resultat}) — un timer en échec reste actif"
            )

    if etat.distants is None:
        dits.append(
            "cohérence hors-site non constatée — le bucket n'a pas répondu ; "
            "seule la copie distante survit à la perte du nœud"
        )
    elif etat.manquants:
        dits.append(
            f"{len(etat.manquants)} instantané(s) absent(s) du hors-site : "
            + ", ".join(etat.manquants[:3])
            + (" …" if len(etat.manquants) > 3 else "")
        )

    return dits


def code_de_sortie(etat: Etat) -> int:
    """0 si tout va bien. Une alarme suffit à sortir en 1."""
    return 1 if alarmes(etat) else 0


# ─── le rendu ────────────────────────────────────────────────────────────────


def _oui_non(valeur: bool | None) -> str:
    if valeur is None:
        return INCONNU
    return "oui" if valeur else "NON"


def _ligne(cle: str, valeur: str) -> str:
    return f"  {cle:<22}{valeur}"


def render_etat(etat: Etat) -> str:
    """Le tableau. Une donnée : il se recopie tel quel, sans horodatage.

    Les deux timers ne vivent pas sur la même machine, et c'est la confusion la
    plus facile à faire dans tout ce montage : chaque ligne dit lequel est où.
    """
    age = INCONNU if etat.age_heures is None else f"{etat.age_heures} h"
    libre = INCONNU if etat.libre_mo is None else f"{etat.libre_mo} Mo"
    distants = INCONNU if etat.distants is None else str(etat.distants)

    lignes = [
        f"CT {etat.ctid} — actif : {_oui_non(etat.ct_actif)}",
        "",
        "sauvegardes locales",
        _ligne("instantanés", str(etat.sauvegardes)),
        _ligne("dernier", etat.dernier or INCONNU),
        _ligne("âge", age),
        _ligne("taille", etat.taille or INCONNU),
        _ligne("espace libre", libre),
        "",
        f"timers — {etat.timer_sauvegarde.nom} vit dans le CT, "
        f"{etat.timer_horssite.nom} sur le nœud",
    ]
    for unite, ou in ((etat.timer_sauvegarde, f"CT {etat.ctid}"),
                      (etat.timer_horssite, "nœud")):
        lignes.append(_ligne(
            f"{unite.nom} ({ou})",
            f"armé : {_oui_non(unite.arme)}  "
            f"dernier : {unite.dernier_resultat or INCONNU}  "
            f"prochain : {unite.prochain or INCONNU}",
        ))

    lignes += [
        "",
        "hors-site",
        _ligne("instantanés distants", distants),
        _ligne("absents du bucket", str(len(etat.manquants))
               if etat.distants is not None else INCONNU),
    ]
    return "\n".join(lignes)


# ─── le constat ──────────────────────────────────────────────────────────────


def relever(ctx) -> Etat:
    """Interroge les deux machines et le bucket. NE MODIFIE RIEN.

    Chaque interrogation qui échoue laisse `None` plutôt qu'une valeur par
    défaut : une valeur inventée ici deviendrait un verdict vert sur un maillon
    qu'on n'a pas su regarder.
    """
    from core.commands import Systemd
    from core.runner import CommandError
    from proxmox import Container

    ctid = ctx.opts.ctid
    conteneur = Container(ctx.runner, ctid)
    try:
        ct_actif = conteneur.running
    except (CommandError, IndexError):
        ct_actif = None

    dans_le_ct = ctx.runner.for_container(ctid)
    sauvegardes, dernier, age, taille = _sauvegardes(dans_le_ct, ctx)
    libre = _libre_mo(dans_le_ct, ctx)

    etat_ct = _unite(Systemd(dans_le_ct), "pg-backup.timer", "pg-backup.service")
    etat_hote = _unite(Systemd(ctx.runner), "pgbk-offsite.timer",
                       "pgbk-offsite.service")

    distants, manquants = _horssite(ctx, conteneur)

    return Etat(
        ctid=ctid,
        ct_actif=ct_actif,
        sauvegardes=sauvegardes,
        dernier=dernier,
        age_heures=age,
        taille=taille,
        libre_mo=libre,
        timer_sauvegarde=etat_ct,
        timer_horssite=etat_hote,
        distants=distants,
        manquants=manquants,
    )


def _unite(systemd, timer: str, service: str) -> UniteEtat:
    """L'état d'un timer ET le résultat de son service.

    Les deux sont nécessaires : le timer dit s'il se déclenchera, le service
    dit ce qui s'est passé la dernière fois. Regarder le seul timer laisse
    passer une unité qui échoue toutes les nuits.
    """
    from core.runner import CommandError

    try:
        arme = systemd.is_enabled(timer)
        actif = systemd.is_active(timer)
        prochain = systemd.next_run(timer)
        resultat = systemd.show(service, "Result")
    except (CommandError, OSError):
        return UniteEtat(timer, None, None)
    return UniteEtat(timer, arme, actif, prochain, resultat)


def _sauvegardes(dans_le_ct, ctx) -> tuple[int, str, int | None, str]:
    """Le dépôt local, lu DANS le conteneur.

    Un seul aller-retour : le nom du dernier instantané, son âge en heures et
    la taille totale. Le script est une constante, les chemins arrivent en
    arguments.
    """
    script = (
        'cd "$1" 2>/dev/null || exit 0\n'
        'set -- $(ls -1d 20*/ 2>/dev/null | grep -v "\\.part/$" | sort)\n'
        '[ $# -eq 0 ] && exit 0\n'
        'echo $#\n'
        'eval dernier=\\${$#}\n'
        'dernier=${dernier%/}\n'
        'echo "$dernier"\n'
        'echo $(( ( $(date +%s) - $(stat -c %Y "$dernier") ) / 3600 ))\n'
        'du -sh . 2>/dev/null | cut -f1\n'
    )
    res = dans_le_ct.read("sh", "-c", script, "sh", ctx.opts.mp2_mount,
                          check=False)
    lignes = res.lines
    if len(lignes) < 4:
        return 0, "", None, ""
    try:
        nombre, age = int(lignes[0]), int(lignes[2])
    except ValueError:
        return 0, "", None, ""
    return nombre, lignes[1], age, lignes[3]


def _libre_mo(dans_le_ct, ctx) -> int | None:
    res = dans_le_ct.read(
        "sh", "-c", 'df -m "$1" | awk "NR==2 {print \\$4}"',
        "sh", ctx.opts.mp2_mount, check=False,
    )
    try:
        return int(res.out)
    except ValueError:
        return None


def _horssite(ctx, conteneur) -> tuple[int | None, tuple[str, ...]]:
    """Ce que le bucket contient, et ce qui lui manque.

    LECTURE SEULE, et c'est structurel : `Rclone` n'expose ni `sync` ni
    `delete`, parce que le compte de service n'a pas `objects.delete`. Un
    objet distant en trop n'est pas notre affaire ; un objet manquant, si.
    """
    import os

    from core.commands import Rclone
    from core.runner import CommandError
    from pgtool.offsite import OffsiteConfig, local_snapshots

    if not ctx.opts.do_offsite:
        return None, ()

    from core.commands import Systemd

    hostname = os.uname().nodename.split(".")[0]
    # L'environnement de l'UNITÉ, pas celui du shell : lancée à la main, la
    # commande n'hérite de rien, et les défauts du code ne décrivent pas le
    # bucket de cette machine. Le drop-in y est inclus, c'est-à-dire ce qui
    # tournera réellement à 3h30.
    try:
        env = dict(os.environ)
        env.update(Systemd(ctx.runner).environment("pgbk-offsite.service"))
        cfg = OffsiteConfig.from_env(env, hostname=hostname)
    except Exception:  # noqa: BLE001 - environnement incomplet : non déterminé
        return None, ()

    try:
        distants = Rclone(ctx.runner, cfg.rclone_config()).list_files(cfg.base)
    except (CommandError, OSError):
        return None, ()

    # Les noms d'instantanés distants sont les premiers segments des chemins.
    noms_distants = {chemin.split("/", 1)[0].rstrip("/")
                     for chemin in distants if chemin}
    try:
        locaux = [p.name for p in local_snapshots(Path(cfg.src))]
    except OSError:
        return len(noms_distants), ()

    manquants = tuple(sorted(n for n in locaux if n not in noms_distants))
    return len(noms_distants), manquants
