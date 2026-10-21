"""Review providers: selection, the request log as the source of truth for paid rounds, and the
repo file turning review on but never off."""
import contextlib, io, json, os, sys, tempfile, unittest

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
sys.path.insert(0, BIN)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet_config  # noqa: E402
import fleet_review as R  # noqa: E402
from test_ship_gates import HEAD, ago, greptile_summary, load_ship, pr, POLICY  # noqa: E402


class Selection(unittest.TestCase):
    def test_names(self):
        self.assertIs(R.provider("greptile"), R.PROVIDERS["greptile"])
        self.assertIsNone(R.provider("none"))
        with self.assertRaises(RuntimeError):
            R.provider("greptlie")  # a typo must not turn reviews off

    def test_review_request_detection(self):
        self.assertTrue(R.is_review_request("@greptileai please re-review the latest commit"))
        self.assertFalse(R.is_review_request("please hold off merging until Monday"))

    def test_finding_bots_cover_every_provider(self):
        for p in R.PROVIDERS.values():
            for login in p.logins:
                self.assertIn(login, R.FINDING_BOTS)


class Tighten(unittest.TestCase):
    def test_repo_file_turns_review_on_never_off(self):
        pol = {"review": "none", "human_paths": [], "merge": "ask"}
        self.assertEqual(fleet_config.tighten(pol, {"ship": {"review": "greptile"}}), [])
        self.assertEqual(pol["review"], "greptile")
        ignored = fleet_config.tighten(pol, {"ship": {"review": "none"}})
        self.assertEqual((pol["review"], [k for k, _ in ignored]), ("greptile", ["ship.review"]))


class Rounds(unittest.TestCase):
    def setUp(self):
        self.S = S = load_ship()
        S.load_policy = lambda r, *a: dict(POLICY, require_selfreview=False)
        S.lines_since = lambda *a: 10_000
        S.reviewer_available = lambda *a: False
        S.required_checks = lambda *a: None
        S.pr_files = lambda o, r, n, p: [f["path"] for f in p["files"]["nodes"]]
        real = R.Greptile.installed
        R.Greptile.installed = lambda *a: True
        self.addCleanup(setattr, R.Greptile, "installed", real)

    def state(self):
        # a score-3 review of an old commit, no request comments on the PR (deleted)
        p = pr(comments=[greptile_summary("0" * 40, 3, ago(60))])
        self.S.fetch = lambda o, r, n: p
        s, _, _ = self.S.evaluate("o", "r", 1)
        return s

    def test_deleted_request_comments_dont_refill_the_budget(self):
        S = self.S
        self.assertEqual(self.state()["review"]["rounds"], 0)
        S.remember_request("o/r", 1, "greptile", HEAD, "https://github.com/o/r/pull/1#issuecomment-1")
        s = self.state()
        self.assertEqual(s["review"]["rounds"], 1)
        self.assertEqual(s["rereviews_left"], POLICY["tiers"]["normal"]["max_rereviews"] - 1)
        self.assertEqual(s["state"], "needs-human")  # normal tier allows 1: spent

    def test_spent_budget_comes_with_a_stuck_report(self):
        S = self.S
        S.remember_request("o/r", 1, "greptile", HEAD, "")
        st = self.state()["stuck"]
        self.assertEqual(st["kind"], "budget")
        self.assertTrue(any("1 of 1 paid re-reviews" in x for x in st["where"]))
        self.assertTrue(any("ship rescue" in x for x in st["options"]))
        self.assertTrue(any("close it" in x for x in st["options"]))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            S.show(self.state(), as_json=False)
        self.assertIn("stuck report:", out.getvalue())
        self.assertIn("likely cause:", out.getvalue())

    def test_report_reads_the_history(self):
        S = self.S
        g = {"findings_by_round": [2, 3, 4], "history": [{"sha": "a", "score": 3}, {"sha": "b", "score": 3}],
             "rounds": 3}
        threads = {"P0": 0, "P1": 1, "P2": 0, "other": 0, "answered": []}
        p = pr()
        p["changedFiles"] = 60
        rep = S.stuck_report("churn", p, g, threads, {"tier": "critical"}, POLICY["tiers"]["critical"],
                             R.PROVIDERS["greptile"])
        causes = " ".join(rep["causes"])
        self.assertIn("creating new findings", causes)
        self.assertIn("score isn't moving", causes)
        self.assertIn("large", causes)
        self.assertTrue(any("split the PR" in x for x in rep["options"]))
        self.assertTrue(any("3 → 3" in x for x in rep["where"]))

    def test_timeout_report_says_to_ask_again_by_hand(self):
        rep = self.S.stuck_report("timeout", pr(), {"findings_by_round": [], "rounds": 1}, {
            "P0": 0, "P1": 0, "P2": 0, "other": 0, "answered": []}, {"tier": "normal"}, POLICY["tiers"]["normal"],
            R.PROVIDERS["greptile"])
        self.assertIn("didn't answer", rep["causes"][0])
        self.assertTrue(any("ask it yourself" in x for x in rep["options"]))

    def test_score_history_one_entry_per_reviewed_commit(self):
        S = self.S
        s1 = self.state()
        S.record(s1)
        s2 = self.state()
        self.assertEqual(len(s2["review"]["history"]), 1)

    def test_requests_are_per_provider(self):
        self.S.remember_request("o/r", 1, "other-bot", HEAD, "")
        self.assertEqual(self.state()["review"]["rounds"], 0)

    def test_unknown_provider_in_policy_stops_ship(self):
        S = self.S
        S.load_policy = lambda r, *a: dict(POLICY, review="greptlie")
        S.fetch = lambda o, r, n: pr()
        with self.assertRaises(SystemExit):
            S.evaluate("o", "r", 1)


if __name__ == "__main__":
    unittest.main()
