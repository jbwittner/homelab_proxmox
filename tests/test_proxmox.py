"""Analyse des sorties Proxmox, et pièges encodés dans les types.

Tous ces cas viennent de pannes réelles ou de bugs trouvés au banc d'essai.
"""

from __future__ import annotations

import pytest

from core.runner import FakeRunner, Result
from proxmox import (
    Container,
    MountPoint,
    Node,
    ProxmoxError,
    Storage,
    Zfs,
    diff_tree,
    parse_storage_status,
    parse_zfs_list,
)

CONFIG_CT = """arch: amd64
hostname: postgresql
protection: 1
features: nesting=1,keyctl=1
mp1: /root/homelab_proxmox/pve-eranikus/pgsql/ct,mp=/etc/pgsql-git,ro=1
mp2: data:subvol-200-disk-0,mp=/var/backups/postgresql,backup=0
startup: order=1
"""


class FauxNoeud(FakeRunner):
    """Un nœud dont les lectures tiennent compte des écritures.

    Sans cela le double serait mensonger : `unprotected()` relit la
    configuration dans son `finally` pour ne pas reposer une protection déjà
    en place, et un `pct config` figé lui ferait croire que le travail est
    fait. Un double qui ignore ses propres écritures valide n'importe quoi.
    """

    def __init__(self, sortie: str = CONFIG_CT) -> None:
        super().__init__()
        self.conf: dict[str, str] = {}
        for ligne in sortie.splitlines():
            cle, _, valeur = ligne.partition(":")
            if cle.strip():
                self.conf[cle.strip()] = valeur.strip()

    def _rendu(self) -> str:
        return "".join(f"{k}: {v}\n" for k, v in self.conf.items())

    def _dispatch(self, argv, *, check, stdin=None, timeout=-1, stream=False):
        argv = tuple(argv)
        if argv[:2] == ("pct", "config"):
            self.calls.append(argv)
            return Result(argv, 0, self._rendu(), "")
        if argv[:2] == ("pct", "set"):
            self.calls.append(argv)
            reste = list(argv[3:])
            while len(reste) >= 2:
                self.conf[reste[0].lstrip("-")] = reste[1]
                reste = reste[2:]
            return Result(argv, 0, "", "")
        return super()._dispatch(
            argv, check=check, stdin=stdin, timeout=timeout, stream=stream
        )


def _ct(sortie: str = CONFIG_CT, **reponses) -> tuple[Container, FauxNoeud]:
    r = FauxNoeud(sortie)
    for fragment, res in reponses.items():
        r.when(fragment, res)
    return Container(r, 200), r


# ─── pct config ──────────────────────────────────────────────────────────────


def test_config_decoupe_sur_le_premier_deux_points():
    """Un volid en contient un (`data:subvol-200-disk-0`). Un séparateur trop
    gourmand lirait la clé « data » et fausserait toutes les comparaisons —
    c'est le bug qu'un `awk -F': *'` avait introduit côté bash."""
    ct, _ = _ct()
    conf = ct.config()
    assert conf["mp2"] == "data:subvol-200-disk-0,mp=/var/backups/postgresql,backup=0"
    assert "data" not in conf


def test_config_lit_les_cles_simples():
    ct, _ = _ct()
    conf = ct.config()
    assert conf["protection"] == "1"
    assert conf["hostname"] == "postgresql"


def test_features_en_ensemble():
    ct, _ = _ct()
    assert ct.features() == {"nesting=1", "keyctl=1"}


# ─── MountPoint ──────────────────────────────────────────────────────────────


def test_render_bind_en_lecture_seule():
    mp = MountPoint(1, "/depot/ct", "/etc/pgsql-git", readonly=True)
    assert mp.render() == "/depot/ct,mp=/etc/pgsql-git,ro=1"
    assert mp.key == "mp1"


def test_render_volume_avec_backup():
    mp = MountPoint(2, "data:50", "/var/backups/postgresql", backup=False)
    assert mp.render() == "data:50,mp=/var/backups/postgresql,backup=0"


def test_matches_insensible_a_lordre_des_options():
    """Proxmox réécrit la valeur qu'on lui donne : comparer les chaînes brutes
    conclurait à une divergence à chaque déploiement, donc à un point de
    montage reposé et à un conteneur redémarré pour rien."""
    mp = MountPoint(1, "/depot/ct", "/etc/pgsql-git", readonly=True)
    assert mp.matches("/depot/ct,mp=/etc/pgsql-git,ro=1")
    assert mp.matches("ro=1,/depot/ct,mp=/etc/pgsql-git")
    assert mp.matches("mp=/etc/pgsql-git,ro=1,/depot/ct")


def test_matches_refuse_une_vraie_divergence():
    mp = MountPoint(1, "/depot/ct", "/etc/pgsql-git", readonly=True)
    assert not mp.matches("/depot,mp=/etc/pgsql-git,ro=1"), "source différente"
    assert not mp.matches("/depot/ct,mp=/etc/pgsql-git"), "ro=1 manquant"
    assert not mp.matches(None) and not mp.matches("")


def test_matches_ne_confond_pas_une_option_supplementaire():
    mp = MountPoint(2, "data:50", "/var/backups", backup=False)
    assert not mp.matches("data:50,mp=/var/backups,backup=0,size=50G")


# ─── protection et points de montage ─────────────────────────────────────────


def test_ensure_mount_signale_quil_faut_redemarrer():
    """Le booléen n'est pas une commodité : un mpN n'est lu qu'au démarrage, et
    poser sans redémarrer donne un répertoire vide, sans message d'erreur."""
    ct, r = _ct()
    mp = MountPoint(1, "/autre", "/etc/pgsql-git", readonly=True)
    assert ct.ensure_mount(mp) is True
    assert any(argv[:2] == ("pct", "set") and "--mp1" in argv for argv in r.calls)


def test_ensure_mount_ne_fait_rien_si_conforme():
    ct, r = _ct()
    mp = MountPoint(
        1, "/root/homelab_proxmox/pve-eranikus/pgsql/ct", "/etc/pgsql-git", readonly=True
    )
    assert ct.ensure_mount(mp) is False
    assert not any(argv[:2] == ("pct", "set") for argv in r.calls)


def test_la_protection_est_levee_puis_remise():
    """L'oublier ne produit aucune erreur et ne se voit pas."""
    ct, r = _ct()
    ct.ensure_mount(MountPoint(1, "/autre", "/etc/pgsql-git", readonly=True))
    sets = [argv for argv in r.calls if argv[:2] == ("pct", "set")]
    assert "--protection" in sets[0] and sets[0][-1] == "0"
    assert "--protection" in sets[-1] and sets[-1][-1] == "1"


def test_la_protection_est_remise_meme_sur_exception():
    ct, r = _ct()
    with pytest.raises(RuntimeError):
        with ct.unprotected():
            raise RuntimeError("boum")
    sets = [argv for argv in r.calls if argv[:2] == ("pct", "set")]
    assert sets[-1][-1] == "1", "le finally doit rétablir la protection"


def test_un_ct_non_protege_nest_pas_touche():
    ct, r = _ct(CONFIG_CT.replace("protection: 1", "protection: 0"))
    with ct.unprotected():
        pass
    assert not any("--protection" in argv for argv in r.calls)


def test_ensure_feature_preserve_les_autres():
    """nesting=1 est obligatoire sur Debian 13 : sans lui les unités qui
    utilisent PrivateTmp échouent en 243/CREDENTIALS, et le conteneur démarre
    en état dégradé sans que rien ne le signale."""
    ct, r = _ct(CONFIG_CT.replace("nesting=1,keyctl=1", "keyctl=1"))
    assert ct.ensure_feature("nesting=1") is True
    pose = [argv for argv in r.calls if "--features" in argv][0]
    assert set(pose[-1].split(",")) == {"nesting=1", "keyctl=1"}


def test_ensure_feature_ne_refait_rien():
    ct, r = _ct()
    assert ct.ensure_feature("nesting=1") is False
    assert not any("--features" in argv for argv in r.calls)


def test_ensure_feature_remplace_une_valeur_contraire():
    ct, _ = _ct(CONFIG_CT.replace("nesting=1,keyctl=1", "nesting=0,keyctl=1"))
    assert ct.ensure_feature("nesting=1") is True


# ─── dépôt d'un arbre ────────────────────────────────────────────────────────


def test_diff_tree_ne_pousse_que_ce_qui_differe():
    local = {"a.py": "1", "b.py": "2"}
    distant = {"a.py": "1", "b.py": "AUTRE"}
    pousser, retirer = diff_tree(local, distant)
    assert pousser == ["b.py"] and retirer == []


def test_diff_tree_est_vide_sur_un_etat_conforme():
    """« Zéro modification sur un état conforme » est le contrôle qui prouve
    que l'outil décrit l'état existant et non un état voisin."""
    table = {"a.py": "1", "b.py": "2"}
    assert diff_tree(table, dict(table)) == ([], [])


def test_diff_tree_elague_ce_que_le_depot_na_plus():
    """Sans élagage, un module renommé laisse son ancêtre, qui continue de
    s'importer : le conteneur tournerait sur du code absent du dépôt."""
    pousser, retirer = diff_tree({"neuf.py": "1"}, {"neuf.py": "1", "ancien.py": "9"})
    assert pousser == [] and retirer == ["ancien.py"]


def test_diff_tree_pousse_ce_qui_manque():
    pousser, retirer = diff_tree({"a.py": "1"}, {})
    assert pousser == ["a.py"] and retirer == []


def test_push_tree_ne_touche_a_rien_si_conforme(tmp_path):
    (tmp_path / "m.py").write_text("contenu")
    import hashlib

    empreinte = hashlib.sha256(b"contenu").hexdigest()
    ct, r = _ct()
    r.when(lambda argv: "sha256sum" in " ".join(argv),
           Result(("sh",), 0, f"{empreinte}  ./m.py\n", ""))
    change = ct.push_tree(tmp_path, "/usr/local/lib/pgtool")
    assert change.changed is False
    assert change.unchanged == 1
    assert not any(argv[:2] == ("pct", "push") for argv in r.calls)


def test_push_tree_pousse_et_elague(tmp_path):
    (tmp_path / "m.py").write_text("contenu")
    ct, r = _ct()
    r.when(lambda argv: "sha256sum" in " ".join(argv),
           Result(("sh",), 0, "0000  ./m.py\ndead  ./vieux.py\n", ""))
    change = ct.push_tree(tmp_path, "/usr/local/lib/pgtool")
    assert change.pushed == ("m.py",)
    assert change.removed == ("vieux.py",)
    assert any(argv[:2] == ("pct", "push") for argv in r.calls)
    assert any("rm" in argv and "/usr/local/lib/pgtool/vieux.py" in argv
               for argv in r.calls)


# ─── pvesm ───────────────────────────────────────────────────────────────────

PVESM = """Name             Type     Status           Total            Used       Available        %
data          zfspool     active       976000000        12000000       964000000    1.23%
local             dir     active        98000000        30000000        68000000   30.61%
sauvegarde        dir   inactive               0               0               0    0.00%
"""


def test_parse_pvesm_status():
    infos = parse_storage_status(PVESM.splitlines())
    assert set(infos) == {"data", "local", "sauvegarde"}
    assert infos["data"].kind == "zfspool"
    assert infos["data"].active is True
    assert infos["data"].avail_kib == 964000000


def test_parse_pvesm_repere_linactif():
    infos = parse_storage_status(PVESM.splitlines())
    assert infos["sauvegarde"].active is False


def test_parse_pvesm_ignore_len_tete_ou_quelle_soit():
    """Reconnaître l'en-tête à son contenu et non à sa position : un pvesm qui
    n'en émettrait pas ferait disparaître le premier stockage réel."""
    sans_entete = PVESM.splitlines()[1:]
    assert "data" in parse_storage_status(sans_entete)


def test_storage_exists():
    r = FakeRunner()
    r.when("pvesm status", Result(("pvesm",), 0, PVESM, ""))
    s = Storage(r)
    assert s.exists("data") and not s.exists("absent")


def test_storage_path_demande_a_proxmox():
    """Déduire le chemin à la main marche jusqu'au jour où le pool change de
    nom."""
    r = FakeRunner()
    r.when("pvesm path", Result(("pvesm",), 0, "/data/subvol-200-disk-0\n", ""))
    assert Storage(r).path("data:subvol-200-disk-0") == "/data/subvol-200-disk-0"


# ─── zfs ─────────────────────────────────────────────────────────────────────

ZFS = "data\t/data\ndata/subvol-200-disk-0\t/data/subvol-200-disk-0\nrpool\tnone\n"


def test_parse_zfs_list_sur_tabulation():
    """`-H` sépare par une tabulation. Découper sur les espaces casserait sur
    un point de montage qui en contient un."""
    table = parse_zfs_list(ZFS.splitlines())
    assert table["data/subvol-200-disk-0"] == "/data/subvol-200-disk-0"
    assert table["rpool"] == "none"


def test_parse_zfs_list_supporte_un_espace_dans_le_chemin():
    table = parse_zfs_list(["pool/x\t/mnt/mon dossier"])
    assert table["pool/x"] == "/mnt/mon dossier"


def test_container_dataset_retrouve_le_volume():
    r = FakeRunner()
    r.when("zfs list", Result(("zfs",), 0, ZFS, ""))
    assert Zfs(r).container_dataset(200) == "/data/subvol-200-disk-0"


def test_container_dataset_absent_rend_none():
    r = FakeRunner()
    r.when("zfs list", Result(("zfs",), 0, ZFS, ""))
    assert Zfs(r).container_dataset(999) is None


def test_un_pool_se_cree_sur_by_id():
    """Les noms /dev/nvmeXnY peuvent permuter au redémarrage, et un pool qui ne
    retrouve pas son disque au boot coûte une matinée."""
    z = Zfs(FakeRunner())
    with pytest.raises(ProxmoxError, match="instable"):
        z.create_pool("data", "/dev/nvme0n1")


def test_un_pool_accepte_un_chemin_stable():
    r = FakeRunner()
    Zfs(r).create_pool("data", "/dev/disk/by-id/nvme-XYZ")
    argv = r.calls[-1]
    assert argv[:2] == ("zpool", "create")
    assert "ashift=12" in argv


# ─── nœud ────────────────────────────────────────────────────────────────────


def test_hostname_est_court():
    """Le drop-in hors-site consigne `hostname -s` : le nom long y ferait une
    arborescence distante différente à chaque déploiement."""
    r = FakeRunner()
    r.when("hostname", Result(("hostname",), 0, "pve-eranikus\n", ""))
    assert Node(r).hostname == "pve-eranikus"
    assert "-s" in r.calls[-1]


def test_ensure_packages_ninstalle_que_ce_qui_manque():
    r = FakeRunner()
    r.when(lambda argv: argv[:2] == ("dpkg", "-s") and argv[2] == "rclone",
           Result(("dpkg",), 1, "", ""))
    poses = Node(r).ensure_packages("sudo", "rclone")
    assert poses == ["rclone"]
    install = [argv for argv in r.calls if argv[:2] == ("apt-get", "install")][0]
    assert "rclone" in install and "sudo" not in install


def test_ensure_packages_ne_fait_rien_si_tout_est_la():
    r = FakeRunner()
    assert Node(r).ensure_packages("sudo") == []
    assert not any(argv[0] == "apt-get" for argv in r.calls)
