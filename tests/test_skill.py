"""The skills and the CLI agree: every command a skill names exists, every fleet subcommand is in
the fleet skill, and `fleet skills install` puts stamped copies where agents read them."""
import glob, json, os, re, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from system_path import SYSTEM_PATH  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
sys.path.insert(0, BIN)
from fleet_skills import SKILLS  # noqa: E402


def skill_dir(name="fleet"):
    return os.path.join(ROOT, "skills", name)


def skill_text(name="fleet"):
    return "\n".join(Path(p).read_text() for p in glob.glob(os.path.join(skill_dir(name), "**", "*.md"), recursive=True))


def offered(tool):
    """Subcommands a tool's help lists, e.g. {'ls', 'doctor', ...} for fleet."""
    if tool == "orch":
        doc = subprocess.run([os.path.join(BIN, "orch")], capture_output=True, text=True).stdout
    else:
        doc = Path(BIN, tool).read_text().split('"""')[1]
    return set(re.findall(rf"^#?\s+{tool} (?:\[[^\]]*\] )*(\w+)", doc, re.M))  # `orch [--json] status`


class Drift(unittest.TestCase):
    def test_skill_names_only_real_commands(self):
        for name in SKILLS:
            text = skill_text(name)
            for tool in ("fleet", "ship", "orch"):
                with self.subTest(skill=name, tool=tool):
                    self.assertEqual(set(re.findall(rf"`{tool} (\w[\w-]*)", text)) - offered(tool), set())

    def test_waits_fit_under_claude_codes_bash_cap(self):
        # Claude Code's Bash tool caps a foreground command at 600000 ms; past it the wait is
        # backgrounded and the agent loses the `next:` line. The skills quote both numbers.
        ship_wait = int(re.search(r"^WAIT_DEFAULT_S = (\d+)", Path(BIN, "ship").read_text(), re.M)[1])
        orch_wait = int(re.search(r"local timeout=(\d+)", Path(BIN, "orch").read_text())[1])
        self.assertEqual(orch_wait, ship_wait)
        self.assertLess(ship_wait * 1000, 600000)
        for name in ("ship", "orchestrate"):
            with self.subTest(skill=name):
                self.assertIn(f"within {ship_wait // 60} minutes" if name == "ship" else f"after {ship_wait // 60} minutes",
                              skill_text(name))
                self.assertIn("600000 ms", skill_text(name))

    def test_every_fleet_subcommand_is_in_the_skill(self):
        text = skill_text()
        missing = {s for s in offered("fleet") if f"fleet {s}" not in text}
        self.assertEqual(missing, set())

    def test_frontmatter(self):
        for name in SKILLS:
            with self.subTest(name):
                head = Path(skill_dir(name), "SKILL.md").read_text().split("---")[1]
                self.assertIn(f"name: {name}", head)
                self.assertIn('version: "{{FLEET_VERSION}}"', head)
                self.assertLess(len(re.search(r"description: (.*)", head).group(1)), 1024)  # agentskills.io limit

    def test_references_exist(self):
        for name in SKILLS:
            text = skill_text(name)
            for ref in set(re.findall(r"`reference/([\w.-]+)`", text)) | set(re.findall(r"`([\w-]+\.md)`", text)):
                if ref in ("SKILL.md",):
                    continue
                with self.subTest(skill=name, ref=ref):
                    self.assertTrue(any(os.path.exists(os.path.join(skill_dir(name), d, ref)) for d in ("", "reference")),
                                    ref)


class Install(unittest.TestCase):
    def run_(self, home, *args):
        env = dict(os.environ, HOME=home, PATH=f"{BIN}:{SYSTEM_PATH}")
        return subprocess.run([os.path.join(BIN, "fleet"), "skills", *args], capture_output=True, text=True,
                              env=env, cwd=home, timeout=60, start_new_session=True)

    def test_install_stamps_a_copy_and_uninstall_takes_it_back(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".claude"))
        os.makedirs(os.path.join(home, ".codex", "skills", "fleet"))  # someone else's "fleet"
        with open(os.path.join(home, ".codex", "skills", "fleet", "SKILL.md"), "w") as f:
            f.write("---\nname: other\n---\n")
        p = self.run_(home, "install", "--yes")
        self.assertEqual(p.returncode, 0, p.stderr)
        dest = os.path.join(home, ".claude", "skills", "fleet")
        self.assertFalse(os.path.islink(dest))
        text = Path(dest, "SKILL.md").read_text()
        self.assertNotIn("{{FLEET_VERSION}}", text)
        self.assertTrue(os.path.exists(os.path.join(dest, "reference", "commands.md")))
        self.assertIn("isn't our fleet skill: left alone", p.stdout)
        orchestrate = os.path.join(home, ".claude", "skills", "orchestrate")
        self.assertIn("name: orchestrate", Path(orchestrate, "SKILL.md").read_text())
        self.assertTrue(os.path.exists(os.path.join(orchestrate, "brief-template.md")))
        self.assertTrue(os.path.exists(os.path.join(home, ".codex", "skills", "orchestrate")))  # not refused there
        self.assertIn("name: other", Path(home, ".codex", "skills", "fleet", "SKILL.md").read_text())
        st = json.loads(self.run_(home, "status", "--json").stdout)
        self.assertTrue(st["harnesses"]["claude"]["current"])
        env = dict(os.environ, HOME=home, PATH=f"{BIN}:{SYSTEM_PATH}")
        u = subprocess.run([os.path.join(BIN, "fleet"), "uninstall", "--yes"], capture_output=True, text=True,
                           env=env, cwd=home, timeout=60)
        self.assertEqual(u.returncode, 0, u.stderr)
        self.assertFalse(os.path.exists(dest))
        self.assertFalse(os.path.exists(orchestrate))
        self.assertTrue(os.path.exists(os.path.join(home, ".codex", "skills", "fleet")))

    def old_install(self, orchestrate):
        """An install from v0.3.0: a fleet copy recorded, and `orchestrate` as given (None: absent,
        "link": a link to the old folder, "foreign": someone else's skill of that name)."""
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".claude"))
        self.assertEqual(self.run_(home, "install", "--to", "claude", "--yes").returncode, 0)
        skills = os.path.join(home, ".claude", "skills")
        dest = os.path.join(skills, "orchestrate")
        shutil.rmtree(dest)
        if orchestrate == "link":
            os.symlink(os.path.join(home, "gone", "orchestrate"), dest)
        elif orchestrate == "foreign":
            os.makedirs(dest)
            Path(dest, "SKILL.md").write_text("---\nname: someone-elses\n---\n")
        env = dict(os.environ, HOME=home, PATH=f"{BIN}:{SYSTEM_PATH}")
        forget = ("import sys; sys.path.insert(0, sys.argv[1]); import fleet_manifest; fleet_manifest.update("
                  "lambda m: m.__setitem__('skills', [s for s in m['skills'] if not s.endswith('orchestrate')]))")
        subprocess.run([sys.executable, "-c", forget, BIN], env=env, check=True, timeout=60)
        refresh = "import sys; sys.path.insert(0, sys.argv[1]); import fleet_skills; fleet_skills.refresh()"
        subprocess.run([sys.executable, "-c", refresh, BIN], env=env, check=True, timeout=60)
        return home, dest, env

    def test_refresh_brings_orchestrate_back_beside_an_old_fleet_install(self):
        for before in (None, "link"):
            with self.subTest(before=before):
                home, dest, env = self.old_install(before)
                self.assertFalse(os.path.islink(dest))
                self.assertIn("name: orchestrate", Path(dest, "SKILL.md").read_text())
                self.assertTrue(json.loads(self.run_(home, "status", "--json").stdout)["harnesses"]["claude"]["current"])
                u = subprocess.run([os.path.join(BIN, "fleet"), "uninstall", "--yes"], capture_output=True, text=True,
                                   env=env, cwd=home, timeout=60)
                self.assertEqual(u.returncode, 0, u.stderr)
                self.assertFalse(os.path.exists(dest))  # recorded, so uninstall takes it back

    def test_refresh_leaves_someone_elses_orchestrate_alone(self):
        home, dest, _ = self.old_install("foreign")
        self.assertIn("name: someone-elses", Path(dest, "SKILL.md").read_text())
        st = json.loads(self.run_(home, "status", "--json").stdout)["harnesses"]["claude"]
        self.assertEqual((st["current"], st["skills"]["orchestrate"]["installed"]), (False, None))

    def test_stale_copy_is_flagged_and_refreshed(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".claude"))
        self.assertEqual(self.run_(home, "install", "--yes").returncode, 0)
        skill = os.path.join(home, ".claude", "skills", "fleet", "SKILL.md")
        text = Path(skill).read_text()
        stamped = re.search(r'version: "([^"]+)"', text).group(1)
        with open(skill, "w") as f:
            f.write(text.replace(stamped, "0.0.1"))
        st = json.loads(self.run_(home, "status", "--json").stdout)["harnesses"]["claude"]
        self.assertEqual((st["installed"], st["current"]), ("0.0.1", False))
        env = dict(os.environ, HOME=home, PATH=f"{BIN}:{SYSTEM_PATH}")
        doc = subprocess.run([os.path.join(BIN, "fleet"), "doctor", "--json", "--only", "skill"], capture_output=True,
                             text=True, env=env, cwd=home, timeout=60)
        c = {x["id"]: x for x in json.loads(doc.stdout)["checks"]}["skill.claude"]
        self.assertEqual((c["status"], c["fix"]), ("warn", "fleet skills install --to claude"))
        code = "import sys; sys.path.insert(0, sys.argv[1]); import fleet_skills; fleet_skills.refresh()"
        subprocess.run([sys.executable, "-c", code, BIN], env=env, check=True, timeout=60)
        self.assertTrue(json.loads(self.run_(home, "status", "--json").stdout)["harnesses"]["claude"]["current"])

    def test_a_missing_to_value_never_means_everywhere(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".claude"))
        self.assertEqual(self.run_(home, "install", "--to", "--yes").returncode, 2)
        self.assertEqual(self.run_(home, "install", "--to=claude", "--yes").returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(home, ".claude", "skills", "fleet")))

    def test_only_frontmatter_counts(self):
        import sys
        sys.path.insert(0, BIN)
        import fleet_skills
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "SKILL.md"), "w") as f:
            f.write("---\nname: other\n---\nAn example:\nname: fleet\nversion: 9\n")
        self.assertIsNone(fleet_skills.installed_version(d))

    def test_install_needs_a_yes(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".claude"))
        self.assertEqual(self.run_(home, "install").returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(home, ".claude", "skills", "fleet")))


if __name__ == "__main__":
    unittest.main()
