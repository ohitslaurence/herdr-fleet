"""Every setting is documented (docs/configuration.md), and settings actually change behaviour."""
import os, re, sys, tempfile, unittest
from pathlib import Path

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "bin"))
import fleet_config  # noqa: E402

OPEN_SECTIONS = ("kinds", "roles", "hints", "repos", "models")  # documented as patterns, not key by key


def leaves(tree, prefix=""):
    for k, v in tree.items():
        if isinstance(v, dict) and v and k not in OPEN_SECTIONS and not prefix.startswith("ship.tiers."):
            yield from leaves(v, f"{prefix}{k}.")
        else:
            yield f"{prefix}{k}"


class Documented(unittest.TestCase):
    def test_after_deploy_cap_is_the_constant(self):
        sys.path.insert(0, os.path.join(ROOT, "tests", "golden"))
        from facts import load_ship
        cap = f"{load_ship().AFTER_DEPLOY_TIMEOUT_S // 60} min"
        doc = Path(ROOT, "docs", "configuration.md").read_text()
        row = next(line for line in doc.splitlines() if line.startswith("| `ship.after_deploy`"))
        self.assertIn(f"up to {cap}", row)
        self.assertIn(f"{cap} cap", " ".join(Path(ROOT, "CHANGELOG.md").read_text().split()))

    def test_every_setting_is_in_the_reference(self):
        doc = Path(ROOT, "docs", "configuration.md").read_text()
        named = set(re.findall(r"`([a-z_]+(?:\.[a-z_<>]+)+)`", doc)) | set(re.findall(r"`([a-z_]+)`", doc))
        for key in leaves(fleet_config.DEFAULTS):
            short = key.split(".")[-1]
            generic = re.sub(r"^ship\.tiers\.[a-z]+", "ship.tiers.<tier>", key)
            with self.subTest(key):
                self.assertTrue(key in named or generic in named or key.split(".")[0] in OPEN_SECTIONS
                                or any(n.startswith(key.split(".")[0] + ".") and short in n for n in named)
                                or short in doc, f"{key} missing from docs/configuration.md")


class Schema(unittest.TestCase):
    def test_validation_tables_name_real_settings(self):
        for key in fleet_config.ENUMS:
            section, name = key.split(".", 1)
            self.assertIn(name, fleet_config.DEFAULTS[section], key)
        for section in fleet_config.NAMED:
            node = fleet_config.DEFAULTS
            for part in section.split("."):
                node = node[part]
            self.assertIsInstance(node, dict, section)


class Effective(unittest.TestCase):
    def configure(self, text):
        path = os.path.join(tempfile.mkdtemp(), "config.toml")
        with open(path, "w") as f:
            f.write(text)
        real = fleet_config.PATH
        fleet_config.PATH = path
        fleet_config._cache.clear()
        self.addCleanup(lambda: (setattr(fleet_config, "PATH", real), fleet_config._cache.clear()))

    def test_ship_policy_carries_the_tunables_per_repo(self):
        self.configure("[ship]\nci_grace_min = 7\n[repos.api]\nrequired_start_min = 99\n")
        pol = fleet_config.ship_policy("api")
        self.assertEqual((pol["ci_grace_min"], pol["required_start_min"]), (7, 99))
        self.assertEqual(fleet_config.ship_policy("web")["required_start_min"], 30)

    def test_deploy_provider_reads_them(self):
        import fleet_deploy
        a = fleet_deploy.provider("actions", "/tmp", {"deploy_start_min": 1, "deploy_timeout_min": 2,
                                                      "deploy_history_pushes": 3})
        self.assertEqual((a.start_min, a.timeout_min, a.history), (1, 2, 3))

    def test_get_prints_one_bare_value(self):
        self.configure("[orch]\nquiet_min = 7\n")
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            fleet_config.main(["get", "orch.quiet_min"])
            fleet_config.main(["get", "roles.security.kind"])
        self.assertEqual(buf.getvalue().split(), ["7", "claude"])
        with self.assertRaises(SystemExit):
            fleet_config.main(["get", "orch.nope"])

    def test_setup_and_layout(self):
        self.configure('[setup]\ninstalls = [["uv.lock", "uv sync"]]\n[layout]\ntabs = ["shell", "agent", "logs"]\n')
        self.assertEqual(fleet_config.get("setup", "installs"), [["uv.lock", "uv sync"]])
        self.assertEqual(fleet_config.get("layout", "tabs"), ["shell", "agent", "logs"])


if __name__ == "__main__":
    unittest.main()
