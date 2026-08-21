"""La distinction lecture / écriture, et ce qui ne doit jamais fuir."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.runner import (
    CommandError,
    FakeRunner,
    Fs,
    InContainer,
    Local,
    Result,
    Runner,
    Secret,
    _mask,
)

# ─── lecture contre écriture ─────────────────────────────────────────────────


def test_lecture_toujours_executee_meme_en_simulation():
    """Un check() qui ne pourrait plus lire n'aurait rien à comparer."""
    r = FakeRunner({"echo bonjour": Result(("echo", "bonjour"), 0, "bonjour\n", "")})
    r.dry_run = True
    res = r.read("echo", "bonjour")
    assert res.out == "bonjour"
    assert res.skipped is False
    assert r.calls == [("echo", "bonjour")]


def test_ecriture_neutralisee_en_simulation(capsys):
    r = FakeRunner()
    r.dry_run = True
    res = r.write("rm", "-rf", "/tmp/x")
    assert res.skipped is True and res.ok
    assert r.calls == [], "aucune commande ne doit avoir été lancée"
    assert "[dry-run] rm -rf /tmp/x" in capsys.readouterr().out


def test_marqueur_de_simulation_unifie(capsys):
    """« [dry-run] », comme pg-deploy.sh et le README. Pas « [simulation] » :
    pendant la migration les deux mondes cohabitent sur le même nœud, et un
    opérateur ne doit pas avoir deux vocabulaires à connaître."""
    r = FakeRunner()
    r.dry_run = True
    r.write("true")
    Fs(dry_run=True).mkdir(Path("/tmp/absent-volontairement"))
    sortie = capsys.readouterr().out
    assert "[simulation]" not in sortie
    assert sortie.count("[dry-run]") == 2


# ─── exécuteurs ──────────────────────────────────────────────────────────────


def test_local_transmet_largv_tel_quel():
    assert Local().build(["ls", "-l"]) == ["ls", "-l"]


def test_in_container_prefixe_par_pct_exec():
    assert InContainer(200).build(["ls", "-l"]) == [
        "pct", "exec", "200", "--", "ls", "-l",
    ]


def test_in_container_se_nomme():
    assert InContainer(200).name == "ct:200"


def test_for_container_conserve_le_mode():
    r = Runner(dry_run=True, timeout=42)
    autre = r.for_container(200)
    assert isinstance(autre.executor, InContainer)
    assert autre.dry_run is True and autre.timeout == 42


def test_aucun_shell_dans_largv_construit():
    """La garantie centrale : pas de chaîne shell, donc pas d'échappement."""
    argv = InContainer(200).build(["psql", "-c", "SELECT 'a b';"])
    assert argv[-1] == "SELECT 'a b';", "l'argument doit traverser intact"


# ─── secrets ─────────────────────────────────────────────────────────────────


def test_un_secret_se_comporte_comme_sa_chaine():
    s = Secret("mot-de-passe")
    assert s == "mot-de-passe"
    assert f"{s}" == "mot-de-passe"


def test_le_secret_est_masque_dans_le_result():
    r = FakeRunner()
    res = r.read("psql", "-v", Secret("password=tres-secret"), "-f", "x.sql")
    assert "***" in res.argv
    assert "tres-secret" not in " ".join(res.argv)


def test_le_secret_ne_fuit_pas_dans_command_error():
    """Sans ça, un CREATE ROLE en échec recopie le mot de passe dans le
    journal : CommandError imprime l'argv complet."""
    echec = Result(("psql", "***"), 1, "", "boum")
    r = FakeRunner()
    r.when(lambda argv: "psql" in argv, echec)
    with pytest.raises(CommandError) as exc:
        r.read("psql", "-v", Secret("password=tres-secret"))
    assert "tres-secret" not in str(exc.value)


def test_le_secret_est_masque_aussi_en_simulation(capsys):
    r = FakeRunner()
    r.dry_run = True
    r.write("psql", "-v", Secret("password=tres-secret"))
    assert "tres-secret" not in capsys.readouterr().out


def test_mask_laisse_les_valeurs_ordinaires():
    assert _mask(["a", Secret("b"), "c"]) == ("a", "***", "c")


# ─── délais ──────────────────────────────────────────────────────────────────


def test_timeout_none_est_une_valeur_demandable():
    """None veut dire « aucune limite » et doit pouvoir être demandé
    explicitement — d'où le sentinelle -1 pour « non précisé ».

    Un rclone copy de 40 min sous le défaut de 300 s remonterait un code 124
    qui n'est dans aucune table de retour.
    """
    vus = {}

    class Espion(FakeRunner):
        def _dispatch(self, argv, *, check, stdin=None, timeout=-1, stream=False):
            vus["timeout"] = self.timeout if timeout == -1 else timeout
            return Result(tuple(argv), 0, "", "")

    e = Espion()
    e.read("x")
    assert vus["timeout"] == 300, "défaut hérité du Runner"
    e.read("x", timeout=None)
    assert vus["timeout"] is None, "None doit survivre, pas retomber sur le défaut"
    e.read("x", timeout=7)
    assert vus["timeout"] == 7


# ─── which ───────────────────────────────────────────────────────────────────


def test_which_dans_un_conteneur_passe_par_un_shell():
    """`command` est une primitive, pas un binaire : `pct exec` fait un execvp
    et ne trouverait rien. Le script est constant, le nom arrive en argument."""
    hote = FakeRunner()
    hote.when(
        lambda argv: "sh" in argv,
        Result(("sh",), 0, "/usr/bin/sudo\n", ""),
    )
    assert hote.for_container(200).which("sudo") == "/usr/bin/sudo"

    argv = hote.calls[0]
    assert argv[:4] == ("pct", "exec", "200", "--")
    commande = argv[4:]
    assert commande[0] == "sh" and commande[1] == "-c"
    assert "sudo" not in commande[2], (
        "le nom cherché ne doit pas être concaténé au script"
    )
    assert commande[-1] == "sudo"


# ─── FakeRunner ──────────────────────────────────────────────────────────────


def test_correspondance_exacte():
    r = FakeRunner({"a b": Result(("a", "b"), 0, "ok\n", "")})
    assert r.read("a", "b").out == "ok"


def test_correspondance_par_fragment():
    """Figer une ligne rclone au caractère près ferait tomber tous les tests au
    premier drapeau ajouté, pour une raison sans rapport avec ce qu'ils
    vérifient."""
    r = FakeRunner()
    r.when("lsf", Result(("rclone",), 0, "a.dump\nMANIFEST\n", ""))
    res = r.read("rclone", "--config", "/x", "--stats", "0", "lsf", "-R", "gcs:b")
    assert res.lines == ["a.dump", "MANIFEST"]


def test_correspondance_par_predicat():
    r = FakeRunner()
    r.when(lambda argv: argv[0] == "zfs", Result(("zfs",), 0, "pool\t/pool\n", ""))
    assert r.read("zfs", "list").out == "pool\t/pool"


def test_defaut_silencieux_si_rien_ne_correspond():
    r = FakeRunner()
    res = r.read("inconnu")
    assert res.ok and res.stdout == ""


def test_un_echec_leve_si_check():
    r = FakeRunner({"faux": Result(("faux",), 1, "", "raté")})
    with pytest.raises(CommandError):
        r.read("faux")
    assert r.read("faux", check=False).code == 1


# ─── Result ──────────────────────────────────────────────────────────────────


def test_lines_ignore_les_lignes_vides():
    res = Result(("x",), 0, "a\n\n  \nb\n", "")
    assert res.lines == ["a", "b"]


def test_out_est_deponctue():
    assert Result(("x",), 0, "  a  \n", "").out == "a"


# ─── Fs ──────────────────────────────────────────────────────────────────────


def test_install_ne_touche_rien_si_identique(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.write_text("contenu")
    fs = Fs()
    assert fs.install(src, dst, mode=0o644) is True
    assert fs.install(src, dst, mode=0o644) is False, "second passage : rien à faire"


def test_install_detecte_un_mode_different(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.write_text("contenu")
    Fs().install(src, dst, mode=0o644)
    assert Fs().install(src, dst, mode=0o755) is True


def test_symlink_idempotent(tmp_path):
    cible = tmp_path / "cible"
    cible.write_text("x")
    lien = tmp_path / "lien"
    fs = Fs()
    assert fs.symlink(cible, lien) is True
    assert fs.symlink(cible, lien) is False


def test_fs_en_simulation_necrit_rien(tmp_path):
    src = tmp_path / "src"
    src.write_text("x")
    dst = tmp_path / "dst"
    assert Fs(dry_run=True).install(src, dst) is True
    assert not dst.exists(), "la simulation ne doit rien écrire"


# ─── écriture de contenu généré ──────────────────────────────────────────────


def test_fs_ecrit_un_contenu_genere_avec_son_mode(tmp_path):
    """Tout ne se copie pas : `rclone.conf` et le drop-in du nœud sont
    fabriqués, pas repris d'un fichier du dépôt. Le mode part avec le contenu —
    un fichier de configuration lisible par tous n'est un défaut qu'à
    retardement."""
    from core.runner import Fs

    cible = tmp_path / "sous" / "dossier" / "f.conf"
    assert Fs().write_file(cible, "[gcs]\n", mode=0o600) is True
    assert cible.read_text() == "[gcs]\n"
    assert (cible.stat().st_mode & 0o777) == 0o600


def test_fs_ne_reecrit_pas_un_contenu_identique(tmp_path):
    """« Zéro modification sur un état conforme » vaut aussi pour le contenu
    généré : réécrire changerait le mtime et ferait mentir le bilan."""
    from core.runner import Fs

    cible = tmp_path / "f.conf"
    Fs().write_file(cible, "a\n", mode=0o600)
    assert Fs().write_file(cible, "a\n", mode=0o600) is False


def test_fs_en_simulation_necrit_rien(tmp_path):
    from core.runner import Fs

    cible = tmp_path / "f.conf"
    assert Fs(dry_run=True).write_file(cible, "a\n") is True
    assert not cible.exists()
