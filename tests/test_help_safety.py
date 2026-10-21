"""Exploring is safe: an agent with no context runs `--help` on everything, so help anywhere must
print help and change nothing (it used to save a note, search GitHub, or start a watcher), and
commands that discard things need an explicit target."""
import os, subprocess, sys, tempfile, unittest
from pathlib import Path
from system_path import SYSTEM_PATH  # noqa: E402

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")


def run(*cmd):
    home = tempfile.mkdtemp()
    env = dict(os.environ, HOME=home, PATH=f"{BIN}:{SYSTEM_PATH}")
    env.pop("HERDR_SOCKET_PATH", None)
    p = subprocess.run([os.path.join(BIN, cmd[0]), *cmd[1:]], capture_output=True, text=True, env=env,
                       cwd=home, timeout=30, stdin=subprocess.DEVNULL)
    created = [os.path.join(r, f) for r, _, fs in os.walk(home) for f in fs]
    return p, created


def fleet_subcommands():
    """Every `fleet <sub>` the help offers: new subcommands are covered without editing this file."""
    import re
    doc = Path(BIN, "fleet").read_text().split('"""')[1]
    return sorted(set(re.findall(r"^  fleet (\w+)", doc, re.M)))


class Help(unittest.TestCase):
    # every `fleet <sub>` in the help, plus the deeper paths that dispatch further
    CASES = [("fleet", "--help"), ("fleet", "ls", "-h"), ("fleet", "new", "help"),
             ("fleet", "keys", "set", "TYPESAFE_API_KEY", "help"), ("fleet", "keys", "unset", "OPENAI_API_KEY", "-h"),
             ("fleet", "server", "install", "--help"), ("fleet", "server", "stop", "help"),
             ("fleet", "server", "uninstall", "-h")] + [
             ("fleet", sub, "--help") for sub in fleet_subcommands()] + [
             ("fleet-watch", "watch", "--help"), ("fleet-watch", "inbox", "done", "--help"),
             ("fleet-cockpit", "help"), ("fleet-cockpit", "note", "--help"),
             ("dev", "done", "--help"), ("ship", "guide", "prc", "help"),
             ("ship", "--help"), ("ship", "pr", "--help"), ("ship", "review", "-h"),
             ("orch", "--help"), ("orch", "start", "x", "--repo", "api", "--help"), ("orch", "wait", "help")]

    def test_help_anywhere_prints_help_and_changes_nothing(self):
        for cmd in self.CASES:
            with self.subTest(" ".join(cmd)):
                p, created = run(*cmd)
                self.assertEqual(p.returncode, 0, p.stderr[-300:])
                self.assertIn("—", p.stdout.splitlines()[0])  # a docstring header, not a listing
                self.assertEqual(created, [])


class ExplicitTargets(unittest.TestCase):
    def test_watch_refuses_unknown_arguments(self):
        for cmd in (("fleet-watch", "watch", "--bogus"), ("fleet-watch", "--once"), ("fleet-watch", "stray")):
            with self.subTest(" ".join(cmd)):
                p, created = run(*cmd)
                self.assertNotEqual(p.returncode, 0)
                self.assertFalse([f for f in created if f.endswith(".lock") or f.endswith(".sock")])

    def test_inbox_done_needs_a_target(self):
        p, _ = run("fleet-watch", "inbox", "done")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--all", p.stderr)

    def test_note_refuses_flags_as_text(self):
        p, created = run("fleet-cockpit", "note", "-x")
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse([f for f in created if "notes.json" in f])


class UsageErrors(unittest.TestCase):
    """A typo must look like a typo (exit 2), never like success or like an unhealthy install."""
    def test_typos_exit_2(self):
        for cmd in (("fleet", "doctr"), ("fleet", "role"), ("orch", "bogus"), ("fleet", "doctor", "--jsonn"),
                    ("fleet", "doctor", "--only", "bogus.check"), ("fleet", "setup", "--propose", "--jsn")):
            with self.subTest(" ".join(cmd)):
                p, _ = run(*cmd)
                self.assertEqual(p.returncode, 2, p.stderr)

    def test_unknown_command_wins_over_a_broken_config(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".config", "fleet"))
        with open(os.path.join(home, ".config", "fleet", "config.toml"), "w") as f:
            f.write("[broken\n")
        p = subprocess.run([os.path.join(BIN, "fleet"), "doctr"], capture_output=True, text=True,
                           env=dict(os.environ, HOME=home), timeout=30)
        self.assertIn("unknown command 'doctr'", p.stderr)


if __name__ == "__main__":
    unittest.main()
