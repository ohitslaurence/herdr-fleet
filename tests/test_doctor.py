"""fleet doctor and config validation: a fresh install can check itself, typos in the config are
caught instead of silently doing nothing, and doctor never prints a key."""
import json, os, re, subprocess, sys, tempfile, tomllib, unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
sys.path.insert(0, BIN)
import fleet_config  # noqa: E402
from system_path import SYSTEM_PATH  # noqa: E402


def problems(data):
    return {p["key"]: (p["level"], p["msg"]) for p in fleet_config.validate(data)}


class Validate(unittest.TestCase):
    def test_typos_warn_with_the_closest_key(self):
        got = problems({"agnets": {"permissions": "bypass"}, "repos": {"api": {"reveiw": "none"}},
                        "dev": {"repos": {"web": {"path": "~/web", "bse": "main"}}}})
        self.assertEqual(got["agnets"], ("warn", "unknown setting; did you mean agents?"))
        self.assertIn("did you mean review?", got["repos.api.reveiw"][1])
        self.assertIn("did you mean base?", got["dev.repos.web.bse"][1])

    def test_invalid_choices_fail(self):
        got = problems({"ship": {"merge": "yolo"}, "agents": {"permissions": "sure"},
                        "decisions": {"watch.offer": "maybe"}, "roles": {"driver": {"kind": "gemini"}},
                        "models": {"kimi": {"harness": "nope", "model": "x"}}})
        for key in ("ship.merge", "agents.permissions", "decisions.watch.offer", "roles.driver.kind",
                    "models.kimi.harness"):
            self.assertEqual(got[key][0], "fail", key)

    def test_wrong_types_fail(self):
        self.assertEqual(problems({"ship": {"ci_grace_min": "3"}})["ship.ci_grace_min"],
                         ("fail", "expected int, got str"))

    def test_valid_config_is_clean(self):
        ok = {"user": {"name": "Ada", "github": "ada"}, "ship": {"merge": "auto", "tiers": {"normal": {"min_score": 3}}},
              "repos": {"api": {"merge": "auto", "protect_paths": ["*.sql"], "tier": "critical",
                                "tiers": {"critical": {"max_rereviews": 2}}}},
              "decisions": {"default": "off", "model": "jev-latest", "watch.offer": "shadow"},
              "hints": {"api": "the public API"}, "dev": {"repos_dir": "~/code", "repos": {"x": {"path": "~/x"}}},
              "roles": {"driver": {"kind": "codex", "model": "gpt", "effort": "high"}},
              "models": {"kimi": {"harness": "opencode", "model": "opencode/kimi-k3", "names": ["kimi"]}},
              "kinds": {"aider": {"cmd": ["aider"], "about": "Aider"}}, "preset": "~/team.toml"}
        self.assertEqual(fleet_config.validate(ok), [])

    def test_readme_sample_config_is_valid(self):
        readme = Path(ROOT, "README.md").read_text()
        blocks = re.findall(r"```toml\n(.*?)```", readme, re.S)
        self.assertTrue(blocks)
        for b in blocks:
            self.assertEqual(fleet_config.validate(tomllib.loads(b)), [], b[:80])


def doctor(*args, config=None, keys=None, keys_mode=0o600):
    home = tempfile.mkdtemp()
    cfg = os.path.join(home, ".config", "fleet")
    os.makedirs(cfg)
    if config is not None:
        with open(os.path.join(cfg, "config.toml"), "w") as f:
            f.write(config)
    if keys is not None:
        p = os.path.join(cfg, "env")
        with open(p, "w") as f:
            f.write(keys)
        os.chmod(p, keys_mode)
    env = dict(os.environ, HOME=home, PATH=f"{BIN}:{SYSTEM_PATH}")
    env.pop("HERDR_SOCKET_PATH", None)
    return subprocess.run([os.path.join(BIN, "fleet"), "doctor", *args], capture_output=True, text=True,
                          env=env, cwd=home, timeout=120, stdin=subprocess.DEVNULL)


class Doctor(unittest.TestCase):
    def checks(self, p):
        return {c["id"]: c for c in json.loads(p.stdout)["checks"]}

    def test_json_shape_and_exit_code(self):
        p = doctor("--json", "--only", "python,config", config="[ship]\nmerge = \"yolo\"\n")
        out = json.loads(p.stdout)
        self.assertEqual(set(out), {"version", "ok", "checks"})
        self.assertFalse(out["ok"])
        self.assertEqual(p.returncode, 1)
        c = self.checks(p)["config.schema.ship.merge"]
        self.assertEqual((c["status"], c["who"]), ("fail", "human"))
        self.assertEqual(set(self.checks(p)), {"python", "config", "config.schema.ship.merge"})

    def test_broken_toml_is_one_failure(self):
        c = self.checks(doctor("--json", "--only", "config", config="[agents\n"))["config"]
        self.assertEqual(c["status"], "fail")
        self.assertIn("config.toml", c["summary"])

    def test_keys_mode_and_never_the_value(self):
        p = doctor("--json", "--only", "keys", keys="TYPESAFE_API_KEY=sk-very-secret-value\n", keys_mode=0o644)
        self.assertNotIn("sk-very-secret-value", p.stdout + p.stderr)
        c = self.checks(p)
        self.assertEqual(c["keys.mode"]["status"], "fail")
        self.assertRegex(c["keys.mode"]["fix"], r"^chmod 600 /.+/\.config/fleet/env$")
        self.assertEqual(c["keys.present"]["detail"]["keys"], {"TYPESAFE_API_KEY": True, "OPENAI_API_KEY": False})

    def test_no_keys_warns(self):
        c = self.checks(doctor("--json", "--only", "keys"))
        self.assertEqual(c["keys.present"]["status"], "warn")
        self.assertNotIn("keys.mode", c)

    def test_missing_herdr_says_how_to_install(self):
        if os.path.exists("/usr/bin/herdr"):
            self.skipTest("herdr is installed system-wide")
        c = self.checks(doctor("--json", "--only", "herdr"))
        self.assertEqual(c["herdr"]["status"], "fail")
        self.assertIn("install herdr", c["herdr"]["fix"])

    def test_text_output_lists_fixes(self):
        p = doctor("--only", "config", config="[agnets]\n")
        self.assertIn("warn  config.schema.agnets", p.stdout)
        self.assertIn("fix (human)", p.stdout)
        self.assertRegex(p.stdout.splitlines()[-1], r"^\d+ ok, \d+ warn, \d+ fail")
        self.assertEqual(p.returncode, 0)  # warnings don't fail

    def test_only_needs_ids(self):
        self.assertEqual(doctor("--only").returncode, 2)


import fleet_doctor as D  # noqa: E402


class Judges(unittest.TestCase):
    MINE = {"version": "0.2.0", "commit": "abc1234"}

    def test_herdr_versions(self):
        self.assertEqual(D.judge_herdr("0.8.9", "0.9.0", "0.9.5", 3)["status"], "fail")
        self.assertEqual(D.judge_herdr("0.9.5", "0.9.0", "0.9.5", 3)["status"], "ok")
        self.assertEqual(D.judge_herdr("0.10.0", "0.9.0", "0.9.5", 3)["status"], "warn")  # 10 > 9, not "0.10" < "0.9"
        self.assertIn("isn't answering", D.judge_herdr("0.9.5", "0.9.0", "0.9.5", "socket gone")["summary"])

    def test_watcher(self):
        now = 1_800_000_000
        fresh = D.time.strftime("%Y-%m-%dT%H:%M:%SZ", D.time.gmtime(now - 60))
        stale = D.time.strftime("%Y-%m-%dT%H:%M:%SZ", D.time.gmtime(now - 3600))
        ok = {"last_pass": fresh, "version": "0.2.0", "commit": "abc1234", "events": True}
        self.assertEqual(D.judge_watcher(ok, self.MINE, 120, now)["status"], "ok")
        self.assertEqual(D.judge_watcher(None, self.MINE, 120, now)["status"], "fail")
        self.assertEqual(D.judge_watcher(RuntimeError("busy"), self.MINE, 120, now)["status"], "fail")
        self.assertEqual(D.judge_watcher(dict(ok, last_pass=stale), self.MINE, 120, now)["status"], "fail")
        old = D.judge_watcher(dict(ok, commit="0000000"), self.MINE, 120, now)
        self.assertEqual((old["status"], old["fix"], old["who"]), ("warn", "fleet server restart", "agent"))

    def test_only_selects_children_not_parents(self):
        self.assertTrue(D.selects("herdr.plugin", ["herdr"]))
        self.assertFalse(D.selects("herdr", ["herdr.plugin"]))
        self.assertTrue(D.may_produce("keys", ["keys.mode"]))

    def test_every_fix_names_a_real_command(self):
        """A remedy that 404s teaches users to distrust doctor."""
        doc = Path(BIN, "fleet").read_text().split('"""')[1]
        offered = set(re.findall(r"^  fleet (\w+)", doc, re.M))
        named = set(re.findall(r"fleet (\w+) (?:install|start|restart|set|status)|run `fleet (\w+)`",
                               Path(BIN, "fleet_doctor.py").read_text()))
        named = {a or b for a, b in named}
        self.assertTrue(named)
        self.assertEqual(named - offered, set())


class Version(unittest.TestCase):
    @unittest.skipUnless(subprocess.run(["git", "-C", BIN, "rev-parse"], capture_output=True).returncode == 0,
                         "a release tarball has no git checkout to read the commit from")
    def test_version(self):
        p = subprocess.run([os.path.join(BIN, "fleet"), "version", "--json"], capture_output=True, text=True, timeout=30)
        v = json.loads(p.stdout)
        self.assertRegex(v["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(v["source"], "checkout")  # tests run from a git checkout
        self.assertRegex(v["commit"], r"^[0-9a-f]{7,}$")
        p = subprocess.run([os.path.join(BIN, "fleet"), "--version"], capture_output=True, text=True, timeout=30)
        self.assertTrue(p.stdout.startswith("herdr-fleet " + v["version"]))


class MissingPrograms(unittest.TestCase):
    def test_checks_skip_when_their_program_is_missing(self):
        p = doctor("--json", "--only", "herdr.keys,codex.sandbox")
        got = {c["id"]: c["status"] for c in json.loads(p.stdout)["checks"]}
        self.assertEqual(got, {"herdr.keys": "skip", "codex.sandbox": "skip"})


if __name__ == "__main__":
    unittest.main()
