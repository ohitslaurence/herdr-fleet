"""`fleet gripe` logs friction with its context, redacted, on this machine; `fleet gripes` reads it
back grouped, and the cockpit counts what no human has read yet."""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from system_path import SYSTEM_PATH  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")


class Gripe(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.repo = os.path.join(self.home, "work")
        os.makedirs(self.repo)
        git = lambda *a: subprocess.run(["git", "-C", self.repo, *a], check=True, capture_output=True)
        git("init", "-q", "-b", "fix-thing")
        git("remote", "add", "origin", "git@github.com:acme/widgets.git")
        orch = os.path.join(self.home, "orch", "w1")
        os.makedirs(orch)
        Path(orch, "meta").write_text("name=w1\nkind=claude\npane=wX:p3\n")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("CODEX_", "CLAUDECODE", "OPENCODE"))}
        self.env.update(HOME=self.home, PATH=f"{BIN}:{SYSTEM_PATH}", ORCH_HOME=os.path.join(self.home, "orch"))

    def fleet(self, *args, **env):
        return subprocess.run([os.path.join(BIN, "fleet"), *args], capture_output=True, text=True,
                              env={**self.env, **env}, cwd=self.repo, timeout=60)

    def logged(self):
        path = Path(self.home, ".local/state/fleet/gripes.jsonl")
        return [json.loads(l) for l in path.read_text().splitlines()]

    def test_logs_text_fields_and_context(self):
        p = self.fleet("gripe", "ship pr said green", "while checks queued", "--tried", "re-ran it",
                       "--cost", "10m", "--fix", "show queued as pending", "--about", "ship",
                       CLAUDECODE="1", HERDR_PANE_ID="wX:p3")
        self.assertEqual(p.returncode, 0, p.stderr)
        [g] = self.logged()
        self.assertEqual({k: g[k] for k in ("text", "tried", "cost", "fix", "about", "kind", "worker", "repo", "branch")},
                         {"text": "ship pr said green while checks queued", "tried": "re-ran it", "cost": "10m",
                          "fix": "show queued as pending", "about": "ship", "kind": "claude", "worker": "w1",
                          "repo": "acme/widgets", "branch": "fix-thing"})
        self.assertEqual(os.stat(Path(self.home, ".local/state/fleet/gripes.jsonl")).st_mode & 0o077, 0)

    def test_secrets_are_redacted_before_writing(self):
        self.assertEqual(self.fleet("gripe", "deploy failed with token=ghp_" + "a" * 36).returncode, 0)
        [g] = self.logged()
        self.assertNotIn("ghp_" + "a" * 36, g["text"])
        self.assertIn("REDACTED", g["text"])

    def test_the_word_help_is_a_gripe_not_a_help_request(self):
        p = self.fleet("gripe", "the", "help", "for", "orch", "is", "vague")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.logged()[0]["text"], "the help for orch is vague")
        self.assertIn("fleet gripe", self.fleet("gripe", "--help").stdout)
        self.assertEqual(len(self.logged()), 1)

    def test_bad_input_is_refused_and_nothing_logged(self):
        for args in ((), ("--tried", "x"), ("text", "--cost"), ("text", "--bogus", "x")):
            with self.subTest(args=args):
                self.assertEqual(self.fleet("gripe", *args).returncode, 2)
        self.assertFalse(Path(self.home, ".local/state/fleet/gripes.jsonl").exists())

    def test_gripes_groups_by_topic_and_marks_seen(self):
        self.fleet("gripe", "orch wait replays old lines", "--about", "orch")
        self.fleet("gripe", "orch log cuts off", "--about", "orch")
        self.fleet("gripe", "tests need docker")
        unseen = lambda: json.loads(self.fleet("gripes", "--json").stdout)["unseen"]
        self.assertEqual(unseen(), 3)  # --json is for agents: it doesn't mark them read
        out = self.fleet("gripes").stdout
        self.assertLess(out.index("orch (2)"), out.index("acme/widgets (1)"))
        self.assertRegex(out, r"(?m)^ \* \S+ \S+  \?: orch wait replays old lines$")
        self.assertRegex(out, r"(?m)^ \* \S+ \S+  \?: tests need docker$")
        self.assertEqual(unseen(), 0)
        self.assertRegex(self.fleet("gripes").stdout, r"(?m)^   \S+ \S+  \?: tests need docker$")

    def test_since_window(self):
        self.fleet("gripe", "old one")
        path = Path(self.home, ".local/state/fleet/gripes.jsonl")
        g = json.loads(path.read_text())
        g["at"] = "2020-01-01T00:00:00+00:00"
        path.write_text(json.dumps(g) + "\n")
        self.assertIn("no gripes in that window", self.fleet("gripes").stdout)
        self.assertIn("old one", self.fleet("gripes", "--all").stdout)
        self.assertIn("old one", self.fleet("gripes", "--since", "520w").stdout)
        self.assertEqual(self.fleet("gripes", "--since", "soon").returncode, 2)

    def test_a_corrupt_seen_mark_is_reported_not_crashed_on(self):
        self.fleet("gripe", "x")
        Path(self.home, ".local/state/fleet/gripes-seen.json").write_text("{nope")
        p = self.fleet("gripes")
        self.assertEqual(p.returncode, 1)
        self.assertIn("not valid JSON", p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        code = ("import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2]); "
                "from test_fleet_gh import load_cockpit; print(load_cockpit().gripes_unseen())")
        out = subprocess.run([sys.executable, "-c", code, BIN, os.path.dirname(__file__)], env=self.env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "None", out.stderr)

    def test_cockpit_counts_unseen(self):
        sys.path.insert(0, BIN)
        from test_fleet_gh import load_cockpit
        cockpit = load_cockpit()
        self.assertEqual(cockpit.gripes_note({}), "")
        self.assertIn("2 new gripes", cockpit.gripes_note({"gripes_unseen": 2}))
        self.assertIn("1 new gripe from", cockpit.gripes_note({"gripes_unseen": 1}))


if __name__ == "__main__":
    unittest.main()
