"""Deploy provider: learned expectations instead of "nothing in a minute = deployed"."""
import os, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin"))
import fleet_deploy as D  # noqa: E402

SHA = "m" * 40


def ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def run(name, status="completed", conclusion="success", minutes=10, event="push"):
    return {"name": name, "status": status, "conclusion": conclusion, "url": f"https://x/{name}", "event": event,
            "createdAt": ago(minutes)}


def history(*workflow_sets):
    """Past pushes to the base, newest first, each running the given workflows."""
    out = []
    for i, names in enumerate(workflow_sets):
        out += [{"workflowName": n, "headSha": f"p{i}", "createdAt": ago(60 * (i + 1))} for n in names]
    return out


class Actions(unittest.TestCase):
    def status(self, runs, base_history, merged_min=30):
        def fake(*args):
            if "--commit" in args:
                return runs
            if "--branch" in args:
                return base_history
            return None
        real, D.gh_json = D.gh_json, fake
        try:
            return D.Actions(tempfile.mkdtemp()).status("o", "r", SHA, ago(merged_min), "main")
        finally:
            D.gh_json = real

    ALWAYS_DEPLOY = history(*[["CI", "Deploy"]] * D.HISTORY_PUSHES)

    def test_expected_workflow_never_started_needs_a_human(self):
        state, detail = self.status([run("CI")], self.ALWAYS_DEPLOY)
        self.assertEqual(state, "needs-human")
        self.assertIn("Deploy", detail)

    def test_nothing_ran_but_something_always_does(self):
        self.assertEqual(self.status([], self.ALWAYS_DEPLOY)[0], "needs-human")

    def test_nothing_ran_and_nothing_ever_does(self):
        self.assertEqual(self.status([], history(*[[]] * D.HISTORY_PUSHES))[0], "deployed")

    def test_path_filtered_workflow_is_not_expected(self):
        # Deploy only ran on some pushes: not "always", so its absence is fine
        h = history(["CI", "Deploy"], ["CI"], ["CI", "Deploy"], ["CI"], ["CI"])
        self.assertEqual(self.status([run("CI")], h)[0], "deployed")

    def test_workflow_added_after_the_merge_is_not_expected(self):
        # every push in history ran Deploy, but all of them came after this (old) merge
        h = [dict(x, createdAt=ago(5)) for x in self.ALWAYS_DEPLOY]
        self.assertEqual(self.status([], h, merged_min=600)[0], "deployed")

    def test_short_history_expects_nothing(self):
        self.assertEqual(self.status([], history(["CI", "Deploy"]))[0], "deployed")

    def test_young_merge_waits(self):
        self.assertEqual(self.status([], self.ALWAYS_DEPLOY, merged_min=1)[0], "deploying")

    def test_running_failed_gated_and_slow(self):
        self.assertEqual(self.status([run("Deploy", "in_progress", None, 5)], self.ALWAYS_DEPLOY)[0], "deploying")
        self.assertEqual(self.status([run("Deploy", conclusion="failure")], self.ALWAYS_DEPLOY)[0], "deploy-failed")
        self.assertEqual(self.status([run("Deploy", "waiting", None)], self.ALWAYS_DEPLOY)[0], "deploy-gated")
        slow = run("Deploy", "in_progress", None, D.TIMEOUT_MIN + 5)
        self.assertEqual(self.status([slow], self.ALWAYS_DEPLOY)[0], "needs-human")

    def test_all_ran_and_passed(self):
        self.assertEqual(self.status([run("CI"), run("Deploy")], self.ALWAYS_DEPLOY), ("deployed", "CI, Deploy"))

    def test_github_down_is_an_error_not_deployed(self):
        real, D.gh_json = D.gh_json, lambda *a: None
        try:
            with self.assertRaises(RuntimeError):
                D.Actions(tempfile.mkdtemp()).status("o", "r", SHA, ago(30), "main")
        finally:
            D.gh_json = real

    def test_provider_names(self):
        self.assertIsInstance(D.provider("none", "/tmp"), D.Off)
        with self.assertRaises(RuntimeError):
            D.provider("actoins", "/tmp")


if __name__ == "__main__":
    unittest.main()
