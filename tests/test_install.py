"""install.sh / fleet update / fleet uninstall in a throwaway HOME against a throwaway repo with
two releases: links only what's ours, updates only between tags after showing the changes,
and uninstall removes exactly what install added."""
import os, re, shutil, subprocess, tempfile, unittest
from pathlib import Path
from system_path import SYSTEM_PATH  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        fake = os.path.join(self.tmp, "fakebin")
        os.makedirs(fake)
        with open(os.path.join(fake, "herdr"), "w") as f:
            f.write("#!/bin/sh\necho \"$@\" >> \"$HOME/herdr.log\"\n")
        os.chmod(os.path.join(fake, "herdr"), 0o755)
        self.env = dict(os.environ, HOME=self.home, PATH=f"{fake}:{SYSTEM_PATH}", **GIT_ENV)
        # origin: v0.1.0, then v0.2.0 with a changelog entry and a skill change
        self.origin = os.path.join(self.tmp, "origin")
        shutil.copytree(ROOT, self.origin, ignore=shutil.ignore_patterns(".git", "tests", "__pycache__"))
        with open(os.path.join(self.origin, "CHANGELOG.md"), "w") as f:
            f.write("# Changelog\n\n## v0.1.0 (2026-10-01)\n\n- first\n")
        self.git(self.origin, "init", "-q", "-b", "main")
        self.git(self.origin, "add", "-A")
        self.git(self.origin, "commit", "-qm", "one")
        self.git(self.origin, "tag", "v0.1.0")
        with open(os.path.join(self.origin, "CHANGELOG.md"), "w") as f:
            f.write("# Changelog\n\n## v0.2.0 (2026-10-20)\n\n- second\n\n## v0.1.0 (2026-10-01)\n\n- first\n")
        with open(os.path.join(self.origin, "skills", "ship", "SKILL.md"), "a") as f:
            f.write("\nA NEW SKILL LINE\n")
        self.git(self.origin, "commit", "-qam", "two")
        self.git(self.origin, "tag", "v0.2.0")
        self.install_dir = os.path.join(self.home, ".local", "share", "herdr-fleet")
        self.git(self.tmp, "clone", "-q", "--branch", "v0.1.0", self.origin, self.install_dir)

    def git(self, cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True, capture_output=True)

    def run_(self, *args, stdin=None):
        return subprocess.run(list(args), capture_output=True, text=True, env=self.env, cwd=self.home, timeout=60,
                              input=stdin, start_new_session=True)

    def fleet(self, *args):
        return self.run_(os.path.join(self.install_dir, "bin", "fleet"), *args)

    def bin(self, name):
        return os.path.join(self.home, ".local", "bin", name)

    def test_install_links_ours_and_leaves_others(self):
        os.makedirs(os.path.dirname(self.bin("dev")))
        with open(self.bin("dev"), "w") as f:
            f.write("someone else's dev\n")
        p = self.run_(os.path.join(self.install_dir, "install.sh"), "--yes")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(os.path.realpath(self.bin("ship")), os.path.realpath(os.path.join(self.install_dir, "bin", "ship")))
        self.assertEqual(Path(self.bin("dev")).read_text(), "someone else's dev\n")
        self.assertIn("left alone (not ours)", p.stdout)
        self.assertIn("plugin link", Path(self.home, "herdr.log").read_text())

    def herdr_log(self):
        path = os.path.join(self.home, "herdr.log")
        return Path(path).read_text() if os.path.exists(path) else ""

    def manifest(self):
        import json
        with open(os.path.join(self.home, ".local", "state", "fleet", "install.json")) as f:
            return json.load(f)

    def build_step(self):
        import tomllib
        with open(os.path.join(self.install_dir, "herdr-plugin.toml"), "rb") as f:
            build = tomllib.load(f)["build"][0]["command"]
        return self.run_(*build[:1], os.path.join(self.install_dir, build[1]), *build[2:])

    def test_herdr_build_step_links_commands_but_not_the_plugin(self):
        p = self.build_step()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.islink(self.bin("ship")))
        self.assertNotIn("plugin link", self.herdr_log())  # herdr owns it
        self.assertEqual(self.manifest()["managed_by"], "herdr")

    def test_herdr_install_updates_through_herdr(self):
        self.build_step()
        self.assertIn("herdr plugin update herdr-fleet", self.fleet("update").stdout)
        import json
        st = json.loads(self.fleet("update", "--json").stdout)  # still JSON on this branch
        self.assertEqual((st["managed_by"], st["up_to_date"]), ("herdr", None))
        self.assertIn("herdr plugin uninstall herdr-fleet", self.fleet("uninstall", "--yes").stdout)

    def test_a_hand_install_takes_updates_back(self):
        self.build_step()
        self.run_(os.path.join(self.install_dir, "install.sh"), "--yes")
        self.assertEqual(self.manifest()["managed_by"], "install.sh")
        self.assertIn("latest release v0.2.0", self.fleet("update", "--check").stdout)

    def test_install_needs_a_yes(self):
        p = self.run_(os.path.join(self.install_dir, "install.sh"))
        self.assertEqual(p.returncode, 2)
        self.assertFalse(os.path.exists(self.bin("ship")))

    def test_relinks_a_link_into_another_checkout(self):
        os.makedirs(os.path.dirname(self.bin("ship")))
        os.symlink("/elsewhere/bin/ship", self.bin("ship"))
        p = self.run_(os.path.join(self.install_dir, "install.sh"), "--yes")
        self.assertIn(f"relink   {self.bin('ship')}", p.stdout)
        self.assertEqual(os.path.realpath(self.bin("ship")), os.path.realpath(os.path.join(self.install_dir, "bin", "ship")))

    def test_help_changes_nothing(self):
        p = self.run_(os.path.join(self.install_dir, "install.sh"), "--help", "--yes")
        self.assertEqual(p.returncode, 0)
        self.assertIn("fleet_install", p.stdout)
        self.assertFalse(os.path.exists(self.bin("ship")))

    def test_uninstall_removes_exactly_what_install_added(self):
        self.run_(os.path.join(self.install_dir, "install.sh"), "--yes")
        os.symlink("/bin/true", self.bin("mine"))
        for keep in (".config/fleet/config.toml", ".local/state/fleet/ledger/x.json"):
            os.makedirs(os.path.dirname(os.path.join(self.home, keep)), exist_ok=True)
            open(os.path.join(self.home, keep), "w").close()
        p = self.fleet("uninstall", "--yes")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(os.path.lexists(self.bin("ship")))
        self.assertTrue(os.path.lexists(self.bin("mine")))
        self.assertIn("plugin unlink herdr-fleet", Path(self.home, "herdr.log").read_text())
        for keep in (".config/fleet/config.toml", ".local/state/fleet/ledger/x.json"):
            self.assertTrue(os.path.exists(os.path.join(self.home, keep)), keep)

    def test_update_refuses_local_changes_before_the_preview(self):
        with open(os.path.join(self.install_dir, "README.md"), "a") as f:
            f.write("local edit\n")
        p = self.fleet("update", "--yes")
        self.assertEqual(p.returncode, 1)
        self.assertIn("local changes", p.stderr)
        self.assertNotIn("A NEW SKILL LINE", p.stdout)

    def test_no_releases_is_not_behind(self):
        for tag in ("v0.1.0", "v0.2.0"):
            self.git(self.origin, "tag", "-d", tag)
            self.git(self.install_dir, "tag", "-d", tag)
        import json
        st = json.loads(self.fleet("update", "--json").stdout)
        self.assertEqual((st["latest"], st["up_to_date"]), (None, None))

    def test_update_json_is_status_only(self):
        p = self.fleet("update", "--json")
        import json
        st = json.loads(p.stdout)
        self.assertEqual((st["current"], st["latest"], st["up_to_date"]), ("v0.1.0", "v0.2.0", False))

    def test_update_shows_changes_then_moves_to_the_tag(self):
        self.run_(os.path.join(self.install_dir, "install.sh"), "--yes")
        check = self.fleet("update", "--check")
        self.assertIn("latest release v0.2.0", check.stdout)
        p = self.fleet("update", "--yes")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("## v0.2.0", p.stdout)
        self.assertNotIn("## v0.1.0", p.stdout)  # only what's new
        self.assertIn("A NEW SKILL LINE", p.stdout)  # skills are shown in full before saying yes
        head = subprocess.run(["git", "-C", self.install_dir, "describe", "--tags"], capture_output=True, text=True).stdout
        self.assertEqual(head.strip(), "v0.2.0")
        self.assertIn("up to date", self.fleet("update", "--check").stdout)

    def test_update_needs_a_terminal_to_say_yes(self):
        p = self.fleet("update")
        self.assertEqual(p.returncode, 2)
        self.assertIn("v0.1.0", subprocess.run(["git", "-C", self.install_dir, "describe", "--tags"],
                                               capture_output=True, text=True).stdout)

    def test_update_never_downgrades_a_branch_checkout(self):
        self.git(self.install_dir, "checkout", "-q", "-b", "dev")
        with open(os.path.join(self.install_dir, "x"), "w") as f:
            f.write("x")
        self.git(self.install_dir, "add", "x")
        self.git(self.install_dir, "commit", "-qm", "local")
        self.git(self.install_dir, "fetch", "-q", "--tags")
        self.git(self.install_dir, "rebase", "-q", "v0.2.0")
        p = self.fleet("update", "--yes")
        self.assertEqual(p.returncode, 1)
        self.assertIn("tracks a branch", p.stderr)


class Release(unittest.TestCase):
    def test_install_docs_name_the_manifest_version_and_the_changelog_has_it(self):
        import tomllib
        with open(os.path.join(ROOT, "herdr-plugin.toml"), "rb") as f:
            tag = "v" + tomllib.load(f)["version"]
        for doc in ("install.sh", "README.md"):
            text = Path(ROOT, doc).read_text()
            self.assertIn(f"--branch {tag} ", text, doc)
            self.assertEqual(set(re.findall(r"\bv\d+\.\d+\.\d+\b", text)), {tag}, doc)  # --ref and every other mention
        log = Path(ROOT, "CHANGELOG.md").read_text()
        self.assertRegex(log, rf"(?m)^## {re.escape(tag)} ")
        for heading in re.findall(r"(?m)^## v.*$", log):
            self.assertRegex(heading, r"^## v\d+\.\d+\.\d+ \((unreleased|untagged|\d{4}-\d{2}-\d{2})\)$")


if __name__ == "__main__":
    unittest.main()