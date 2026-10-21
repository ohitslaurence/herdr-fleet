"""A fresh install has none of the author's machine: no ~/.agents skills, maybe no gritty, maybe
no herdr running, maybe a typo in the config. Agents follow what the tools print, so every
pointer must resolve and every failure must say so instead of looking like an empty fleet."""
import importlib.machinery, importlib.util, os, re, subprocess, tempfile, unittest
from pathlib import Path
from system_path import SYSTEM_PATH  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")


def load(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), os.path.join(BIN, name))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(m)
    return m


def run(*cmd, herdr=None, config=None):
    """cmd in a fresh HOME. herdr: None = not on PATH, else the body of a fake herdr script."""
    home = tempfile.mkdtemp()
    fake = os.path.join(home, "fakebin")
    os.makedirs(fake)
    if herdr is not None:
        with open(os.path.join(fake, "herdr"), "w") as f:
            f.write("#!/bin/sh\n" + herdr + "\n")
        os.chmod(os.path.join(fake, "herdr"), 0o755)
    if config is not None:
        os.makedirs(os.path.join(home, ".config", "fleet"))
        with open(os.path.join(home, ".config", "fleet", "config.toml"), "w") as f:
            f.write(config)
    env = dict(os.environ, HOME=home, PATH=f"{fake}:{BIN}:{SYSTEM_PATH}")
    env.pop("HERDR_SOCKET_PATH", None)
    return subprocess.run([os.path.join(BIN, cmd[0]), *cmd[1:]], capture_output=True, text=True, env=env,
                          cwd=home, timeout=30, stdin=subprocess.DEVNULL)


class Guides(unittest.TestCase):
    ship = load("ship")

    def test_every_topic_prints_its_section(self):
        for topic, (_, heading) in self.ship.GUIDES.items():
            with self.subTest(topic):
                text = self.ship.guide_text(topic)
                self.assertTrue(text.startswith(heading or "#"), text[:80])
                self.assertNotIn("\n## ", text if heading else "")  # one section, not the rest of the file
                self.assertGreater(len(text), 200)

    def test_every_pointer_names_a_topic(self):
        """`ship guide X` in a next: line or a skill must exist: an agent runs it verbatim."""
        files = [os.path.join(BIN, "ship")] + [os.path.join(r, f) for r, _, fs in os.walk(os.path.join(ROOT, "skills"))
                                               for f in fs]
        named = set()
        for path in files:
            named |= set(re.findall(r"ship guide ([a-z-]+)", Path(path).read_text()))
        self.assertTrue(named)
        self.assertEqual(named - set(self.ship.GUIDES), set())

    def test_no_author_machine_paths(self):
        checked = 0
        for top in ("bin", "skills", "docs"):
            for r, _, fs in os.walk(os.path.join(ROOT, top)):
                for f in fs:
                    with self.subTest(f), open(os.path.join(r, f), errors="ignore") as fh:
                        # a path into one machine's skills; the harness dir itself (doctor scans it) is fine
                        self.assertNotRegex(fh.read(), r"~/\.agents/skills/[a-z]")
                    checked += 1
        self.assertGreater(checked, 20)

    def test_replies_is_the_reactions_section(self):
        out = run("ship", "guide", "replies").stdout
        self.assertTrue(out.startswith("### Reacting to comments"), out[:80])
        self.assertNotEqual(out, run("ship", "guide", "prc").stdout)

    def test_unknown_topic_lists_topics(self):
        p = run("ship", "guide", "nope")
        self.assertEqual(p.returncode, 2)
        self.assertIn("self-review", p.stderr)


class NoGritty(unittest.TestCase):
    def test_commit_hint_falls_back_to_git_and_gh(self):
        ship = load("ship")
        real = ship.shutil.which
        try:
            ship.shutil.which = lambda c: None
            self.assertIn("gh pr create --fill", ship.commit_hint())
            ship.shutil.which = lambda c: "/x/gritty"
            self.assertIn("gritty pr --accept", ship.commit_hint())
        finally:
            ship.shutil.which = real


class HerdrUnreachable(unittest.TestCase):
    def test_ls_fails_when_herdr_is_missing(self):
        p = run("fleet-cockpit", "ls")
        self.assertEqual(p.returncode, 1)
        self.assertIn("herdr unreachable: herdr isn't on PATH", p.stderr)

    def test_ls_fails_when_herdr_errors(self):
        p = run("fleet-cockpit", "ls", herdr="echo 'no server running' >&2; exit 1")
        self.assertEqual(p.returncode, 1)
        self.assertIn("herdr unreachable: no server running", p.stderr)

    def test_running_herdr_with_no_agents_is_fine(self):
        for cmd in (("fleet-cockpit", "ls"), ("fleet-watch", "watch", "--once", "--dry-run")):
            with self.subTest(cmd[0]):
                p = run(*cmd, herdr='echo \'{"result":{"agents":[],"workspaces":[],"panes":[]}}\'')
                self.assertEqual(p.returncode, 0, p.stderr[-300:])
                self.assertNotIn("unreachable", p.stderr)

    def test_garbage_from_herdr_is_unreachable_not_a_crash(self):
        p = run("fleet-cockpit", "ls", herdr="echo 'not json'")
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("unexpected answer from `herdr agent list`", p.stderr)

    def test_watch_once_fails_when_herdr_errors(self):
        p = run("fleet-watch", "watch", "--once", "--dry-run", herdr="exit 3")
        self.assertEqual(p.returncode, 1)
        self.assertIn("herdr unreachable: `herdr agent list` exited 3", p.stderr)


class BadConfig(unittest.TestCase):
    def test_one_line_not_a_traceback(self):
        for cmd in (("fleet-cockpit", "ls"), ("fleet-watch", "inbox"), ("ship", "policy", "o/r")):
            with self.subTest(cmd[0]):
                p = run(*cmd, herdr='echo \'{"result":{"agents":[]}}\'', config="[agents\n")
                self.assertEqual(p.returncode, 1)
                self.assertNotIn("Traceback", p.stderr)
                self.assertRegex(p.stderr, r"^fleet: .*config\.toml")


class RulesFallback(unittest.TestCase):
    def test_says_when_haiku_was_not_used(self):
        new = load("fleet-new")
        real = new.subprocess.run

        def missing(*a, **kw):
            raise FileNotFoundError(2, "No such file or directory", "claude")
        try:
            new.subprocess.run = missing
            d = new.classify("api, fix the login test", ["api"])
        finally:
            new.subprocess.run = real
        self.assertEqual(d["repos"], ["api"])
        self.assertTrue(d["via"].startswith("rules (haiku unavailable:"), d["via"])


if __name__ == "__main__":
    unittest.main()
