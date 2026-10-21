"""fleet setup: proposals come from what's on the machine, never loosen safety, never rewrite
what the user chose, and only a terminal can write the file."""
import json, os, subprocess, tempfile, unittest
from system_path import SYSTEM_PATH  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")


def setup(*args, config=None, repos=(), stdin=None):
    home = tempfile.mkdtemp()
    for r in repos:
        os.makedirs(os.path.join(home, r, ".git"))
    if config is not None:
        os.makedirs(os.path.join(home, ".config", "fleet"))
        with open(os.path.join(home, ".config", "fleet", "config.toml"), "w") as f:
            f.write(config)
    env = dict(os.environ, HOME=home, PATH=f"{BIN}:{SYSTEM_PATH}", GIT_CONFIG_GLOBAL="/dev/null")
    p = subprocess.run([os.path.join(BIN, "fleet"), "setup", *args], capture_output=True, text=True, env=env,
                       cwd=home, timeout=60, input=stdin, start_new_session=True)  # no controlling terminal
    return p, home


class Propose(unittest.TestCase):
    def test_finds_the_repos_folder_one_level_down(self):
        p, _ = setup("--propose", "--json", repos=("code/a", "code/b", "dev/org/x", "dev/org/y", "dev/org/z"))
        out = json.loads(p.stdout)
        self.assertEqual(out["proposed"]["dev"]["repos_dir"], "~/dev/org")
        self.assertIn("dev.repos_dir", out["ask"])  # two folders have repos: the human picks
        self.assertIn("~/code has 2", out["evidence"]["dev.repos_dir"])

    def test_never_proposes_looser_safety(self):
        out = json.loads(setup("--propose", "--json")[0].stdout)
        self.assertNotIn("bypass", json.dumps(out["proposed"]))
        self.assertIn("agents.permissions", out["ask"])
        self.assertIn("agents.permissions", out["never_proposed"])

    def test_keeps_what_the_user_set(self):
        cfg = '[dev]\nrepos_dir = "~/elsewhere"\n'
        out = json.loads(setup("--propose", "--json", config=cfg, repos=("code/a",))[0].stdout)
        self.assertNotIn("dev", out["proposed"])
        self.assertTrue(out["toml"].startswith(cfg.rstrip()))

    def test_proposal_is_valid_toml_config(self):
        import sys, tomllib
        sys.path.insert(0, BIN)
        import fleet_config
        out = json.loads(setup("--propose", "--json", repos=("code/a",))[0].stdout)
        self.assertEqual(fleet_config.validate(tomllib.loads(out["toml"])), [])


class Apply(unittest.TestCase):
    def test_needs_a_terminal_to_write(self):
        p, home = setup("apply", "-", stdin='[ship]\nmerge = "ask"\n')
        self.assertEqual(p.returncode, 2)
        self.assertIn("needs a terminal", p.stderr)
        self.assertFalse(os.path.exists(os.path.join(home, ".config", "fleet", "config.toml")))

    def test_terminal_refusal_names_the_command_to_run(self):
        home = tempfile.mkdtemp()
        path = os.path.join(home, "proposal.toml")
        with open(path, "w") as f:
            f.write('[ship]\nmerge = "ask"\n')
        p, _ = setup("apply", path)
        self.assertEqual(p.returncode, 2)
        self.assertIn(f"run `fleet setup apply {path}` in your own terminal", p.stderr)

    def test_invalid_config_is_refused_before_asking(self):
        p, _ = setup("apply", "-", stdin='[ship]\nmerge = "yolo"\n')
        self.assertEqual(p.returncode, 1)
        self.assertIn("ship.merge", p.stderr)

    def test_wizard_needs_a_terminal(self):
        self.assertEqual(setup()[0].returncode, 2)


if __name__ == "__main__":
    unittest.main()
