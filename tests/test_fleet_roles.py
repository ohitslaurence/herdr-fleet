"""fleet role: any harness orch can start (claude, codex, opencode) is a valid kind; "opposite"
pairs only the two review families, claude and codex."""
import os, subprocess, tempfile, textwrap, unittest

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")


def fleet(config, *args):
    home = tempfile.mkdtemp()
    os.makedirs(os.path.join(home, ".config", "fleet"))
    with open(os.path.join(home, ".config", "fleet", "config.toml"), "w") as f:
        f.write(textwrap.dedent(config))
    return subprocess.run([os.path.join(BIN, "fleet"), *args], capture_output=True, text=True,
                          env=dict(os.environ, HOME=home), timeout=30)


class Roles(unittest.TestCase):
    CFG = """
        [roles.worker]
        kind = "opencode"
        model = "opencode/kimi-k3"
        """

    def test_opencode_worker(self):
        p = fleet(self.CFG, "role", "worker")
        self.assertEqual((p.returncode, p.stdout.split()), (0, ["opencode", "opencode/kimi-k3", "-"]))

    def test_opposite_stays_claude_codex(self):
        self.assertEqual(fleet(self.CFG, "role", "reviewer", "--author", "claude").stdout.split()[0], "codex")
        self.assertNotEqual(fleet(self.CFG, "role", "reviewer", "--author", "opencode").returncode, 0)
        self.assertNotIn("author opencode", fleet(self.CFG, "roles").stdout)

    def test_unknown_kind_is_an_error(self):
        self.assertNotEqual(fleet('[roles.worker]\nkind = "vim"\n', "role", "worker").returncode, 0)


if __name__ == "__main__":
    unittest.main()
