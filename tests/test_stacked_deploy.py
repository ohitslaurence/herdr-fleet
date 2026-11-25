"""A stacked PR (merged into another PR's branch) is deployed only once that branch's code reaches
the default branch: ship follows the PR whose head is its base instead of watching its own merge."""
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin"))
from facts import load_ship  # noqa: E402
import fleet_deploy  # noqa: E402


def pr(n, state, merged_at=None, base="main"):
    return {"number": n, "state": state, "mergedAt": merged_at, "baseRefName": base,
            "mergeCommit": {"oid": f"sha{n}"} if merged_at else None}


class StackedDeploy(unittest.TestCase):
    def setUp(self):
        self.ship = load_ship()
        self.heads = {}     # branch -> PRs with that head
        self.watched = []   # (sha, base) the deploy provider was asked about
        self.gh_calls = []

        def gh_json(*args):
            self.gh_calls.append(args)
            if args[:2] == ("api", "repos/o/r"):
                return {"b": "main"}
            if args[:2] == ("pr", "list"):
                return self.heads.get(args[args.index("--head") + 1], [])
            raise AssertionError(f"unexpected gh call {args}")
        self.ship.gh_json = gh_json

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

    def test_base_pr_still_open_is_done_here_and_names_the_pr_that_carries_it(self):
        self.heads["feat-a"] = [pr(4, "MERGED", "2026-11-19T09:00:00Z"), pr(5, "OPEN")]
        st, why = self.deploy("feat-a")
        self.assertEqual(st, "deployed")
        self.assertIn("when #5 merges", why)
        self.assertEqual(self.watched, [])

    def test_base_pr_closed_unmerged_needs_a_human(self):
        self.heads["feat-a"] = [pr(5, "CLOSED")]
        st, why = self.deploy("feat-a")
        self.assertEqual(st, "needs-human")
        self.assertIn("closed unmerged", why)

    def test_a_long_lived_branch_with_no_pr_is_watched_directly(self):
        self.assertEqual(self.deploy("staging"), ("deployed", "Deploy"))
        self.assertEqual(self.watched, [("child", "staging")])

    def test_the_default_branch_is_looked_up_once_per_repo(self):
        self.deploy("main")
        self.deploy("main")
        self.assertEqual(sum(a[:2] == ("api", "repos/o/r") for a in self.gh_calls), 1)


if __name__ == "__main__":
    unittest.main()
