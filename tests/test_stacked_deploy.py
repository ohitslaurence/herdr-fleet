"""A stacked PR (merged into another PR's branch) is deployed only once that branch's code reaches
the default branch: ship follows the PR whose head is its base instead of watching its own merge."""
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin"))
from facts import load_ship  # noqa: E402
import fleet_deploy, fleet_proc  # noqa: E402


def pr(n, state, merged_at=None, base="main"):
    return {"number": n, "state": state, "mergedAt": merged_at, "baseRefName": base,
            "mergeCommit": {"oid": f"sha{n}"} if merged_at else None}


class StackedDeploy(unittest.TestCase):
    def setUp(self):
        self.ship = load_ship()
        self.heads = {}     # branch -> PRs with that head
        self.watched = []   # (sha, base) the deploy provider was asked about

        def gh_json(*args):
            if args[:2] == ("pr", "list"):
                return self.heads.get(args[args.index("--head") + 1], [])
            raise AssertionError(f"unexpected gh call {args}")
        self.ship.gh_json = gh_json
        real_default = fleet_proc.default_branch
        fleet_proc.default_branch = lambda o, r: self.default
        self.addCleanup(setattr, fleet_proc, "default_branch", real_default)
        self.default = "main"

        test = self

        class Provider:
            def status(self, o, r, sha, merged_at, base):
                test.watched.append((sha, base))
                return "deployed", "Deploy"
        real, fleet_deploy.provider = fleet_deploy.provider, lambda *a, **k: Provider()
        self.addCleanup(setattr, fleet_deploy, "provider", real)

    def deploy(self, base, merged_at="2026-11-20T10:00:00Z"):
        return self.ship.deploy_of("o", "r", "child", merged_at, base, {"deploy": "actions"})

    def test_a_pr_into_the_default_branch_watches_its_own_merge(self):
        self.assertEqual(self.deploy("main"), ("deployed", "Deploy"))
        self.assertEqual(self.watched, [("child", "main")])

    def test_merged_into_a_branch_whose_pr_merged_earlier_never_reached_main(self):
        self.heads["feat-a"] = [pr(5, "MERGED", "2026-11-20T09:00:00Z")]
        st, why = self.deploy("feat-a")
        self.assertEqual(st, "needs-human")
        self.assertIn("never reached main", why)
        self.assertIn("#5", why)
        self.assertEqual(self.watched, [])

    def test_merged_before_its_base_pr_follows_that_pr_deploy(self):
        self.heads["feat-a"] = [pr(5, "MERGED", "2026-11-20T11:00:00Z")]
        self.assertEqual(self.deploy("feat-a"), ("deployed", "via #5: Deploy"))
        self.assertEqual(self.watched, [("sha5", "main")])

    def test_a_three_level_stack_is_followed_to_the_top(self):
        self.heads["feat-b"] = [pr(6, "MERGED", "2026-11-20T11:00:00Z", base="feat-a")]
        self.heads["feat-a"] = [pr(5, "MERGED", "2026-11-20T12:00:00Z")]
        self.assertEqual(self.deploy("feat-b"), ("deployed", "via #6: via #5: Deploy"))
        self.assertEqual(self.watched, [("sha5", "main")])

    def test_base_pr_still_open_is_its_own_state_naming_the_pr_that_carries_it(self):
        self.heads["feat-a"] = [pr(4, "MERGED", "2026-11-19T09:00:00Z"), pr(5, "OPEN")]
        st, why = self.deploy("feat-a")
        self.assertEqual(st, "merged-into-base")
        self.assertIn("when #5 merges", why)
        self.assertEqual(self.watched, [])

    def test_the_first_base_pr_merged_after_the_merge_carries_it(self):
        self.heads["feat-a"] = [pr(7, "MERGED", "2026-11-20T15:00:00Z"), pr(4, "MERGED", "2026-11-19T09:00:00Z"),
                                pr(6, "MERGED", "2026-11-20T11:00:00Z"), pr(3, "CLOSED")]
        self.assertEqual(self.deploy("feat-a"), ("deployed", "via #6: Deploy"))
        self.assertEqual(self.watched, [("sha6", "main")])

    def test_base_pr_closed_unmerged_needs_a_human(self):
        self.heads["feat-a"] = [pr(5, "CLOSED")]
        st, why = self.deploy("feat-a")
        self.assertEqual(st, "needs-human")
        self.assertIn("closed unmerged", why)

    def test_a_long_lived_branch_with_no_pr_is_watched_directly(self):
        self.assertEqual(self.deploy("staging"), ("deployed", "Deploy"))
        self.assertEqual(self.watched, [("child", "staging")])

    def test_a_cycle_of_branches_stops_at_the_depth_limit(self):
        self.heads["feat-a"] = [pr(5, "MERGED", "2026-11-20T11:00:00Z", base="feat-b")]
        self.heads["feat-b"] = [pr(6, "MERGED", "2026-11-20T11:00:00Z", base="feat-a")]
        st, why = self.deploy("feat-a")
        self.assertEqual(st, "needs-human")
        self.assertIn("check by hand", why)
        self.assertEqual(self.watched, [])

    def test_an_unreadable_default_branch_is_an_error_not_a_guess(self):
        self.default = None
        with self.assertRaises(RuntimeError):
            self.deploy("feat-a")

    def test_the_new_state_is_terminal_and_documented_for_agents(self):
        self.assertIn("merged-into-base", self.ship.TERMINAL)
        skill = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "ship", "SKILL.md")).read()
        self.assertIn("| `merged-into-base` |", skill)


class DefaultBranch(unittest.TestCase):
    def test_asked_once_per_repo_and_a_failure_is_not_remembered(self):
        answers, calls = [None, "main\n"], []

        def gh_text(*args, timeout=None):
            calls.append(args)
            return answers.pop(0) if answers else "other\n"
        real, fleet_proc.gh_text = fleet_proc.gh_text, gh_text
        self.addCleanup(setattr, fleet_proc, "gh_text", real)
        fleet_proc._DEFAULT_BRANCH.pop(("x", "y"), None)
        self.assertIsNone(fleet_proc.default_branch("x", "y"))
        self.assertEqual(fleet_proc.default_branch("x", "y"), "main")
        self.assertEqual(fleet_proc.default_branch("x", "y"), "main")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
