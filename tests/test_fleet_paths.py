"""Worktree path and branch templates: defaults are the historical shapes; custom layouts work;
ownership checks never match spares or review checkouts."""
import os, re, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
import fleet_paths as P  # noqa: E402


def use(**t):
    P._cache["t"] = dict(P.DEFAULTS, **t)


class Templates(unittest.TestCase):
    def tearDown(self):
        P._cache.clear()

    def test_defaults_are_the_old_shapes(self):
        use()
        self.assertEqual(P.worktree_path("/r/platform", "platform", "fix"), "/r/platform-wt-fix")
        self.assertEqual(P.pr_worktree_path("/r/platform", "platform", 12), "/r/platform-wt-pr-12")
        self.assertEqual(P.branch_name("platform", "fix"), "platform__fix")

    def test_custom_layouts(self):
        use(path="{repo_path}/.worktrees/{task}", branch="{user}/{task}")
        os.environ["FLEET_USER"] = "lau"
        try:
            self.assertEqual(P.worktree_path("/r/web", "web", "pay"), "/r/web/.worktrees/pay")
            self.assertEqual(P.branch_name("web", "pay"), "lau/pay")
            self.assertEqual(P.branch_task("lau/pay", "web"), "pay")
            self.assertIsNone(P.branch_task("someone/pay", "web"))
        finally:
            del os.environ["FLEET_USER"]

    def test_ownership(self):
        use()
        root = os.path.realpath(tempfile.mkdtemp())
        repo = os.path.join(root, "platform")
        self.assertEqual(P.task_of(repo + "-wt-fix", repo, "platform"), "fix")
        self.assertEqual(P.task_of(repo + "-wt-pr-7", repo, "platform"), "pr-7")
        for not_ours in (os.path.join(root, ".spares", "platform"), os.path.join(root, "review-wt", "platform-1-ab"),
                         repo, os.path.join(root, "web-wt-fix"), repo + "-wt-a/b"):
            self.assertFalse(P.dev_owned(not_ours, repo, "platform"), not_ours)
        self.assertEqual(P.branch_task("platform__fix", "platform"), "fix")
        self.assertIsNone(P.branch_task("web__fix", "platform"))
        self.assertIsNone(P.branch_task("main", "platform"))

    def test_external_layout_ownership(self):
        use(path="~/wt/{repo}/{task}")
        home = os.path.expanduser("~")
        self.assertEqual(P.worktree_path("/r/platform", "platform", "x"), f"{home}/wt/platform/x")
        self.assertTrue(P.dev_owned(f"{home}/wt/platform/x", "/r/platform", "platform"))
        self.assertFalse(P.dev_owned(f"{home}/wt/web/x", "/r/platform", "platform"))


class Linked(unittest.TestCase):
    def test_is_linked_worktree(self):
        root = tempfile.mkdtemp()
        repo, wt = os.path.join(root, "r"), os.path.join(root, "r-wt-x")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        run = lambda *a: subprocess.run(["git", *a], check=True, capture_output=True, env=env)
        run("init", "-q", "-b", "main", repo)
        run("-C", repo, "commit", "-q", "--allow-empty", "-m", "x")
        run("-C", repo, "worktree", "add", "-q", wt, "-b", "x")
        self.assertTrue(P.is_linked_worktree(wt))
        self.assertTrue(P.is_linked_worktree(os.path.join(wt)))
        self.assertFalse(P.is_linked_worktree(repo))
        self.assertFalse(P.is_linked_worktree(root))


class NoHooks(unittest.TestCase):
    def test_every_new_worktree_is_made_with_hooks_off(self):
        """dev's tests run a failing hook; every other tool is held to it here: a call that makes or
        switches a worktree turns hooks off (fleet_paths.NO_HOOKS says why). A call may wrap after
        `worktree`, so each line is read with the next."""
        bin_dir = os.path.join(os.path.dirname(__file__), "..", "bin")
        makes = re.compile(r'worktree"?,?\s+"?add|"checkout", "-q", "-B"')
        seen = 0
        for name in sorted(os.listdir(bin_dir)):
            path = os.path.join(bin_dir, name)
            if not os.path.isfile(path):
                continue
            with open(path, errors="replace") as f:
                lines = [line for line in f if not line.lstrip().startswith("#")]
            for n, (line, nxt) in enumerate(zip(lines, lines[1:] + [""]), 1):
                if makes.search(line.rstrip() + " " + nxt.strip()) and "def " not in line:
                    seen += 1
                    with self.subTest(f"{name}: {line.strip()}"):
                        self.assertRegex(line + nxt, r"NO_HOOKS|core\.hooksPath=/dev/null")
        self.assertGreaterEqual(seen, 6)  # dev x2, fleet-new x2, orch x2, ship: the scan finds them

if __name__ == "__main__":
    unittest.main()
