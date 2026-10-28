"""ship's gates on synthetic PRs (no network): truncation, CI grace, answered P0/P1, retag
lock, GitHub errors. Rebuilds the synthetic harness lost with the 2026-10-13 scratchpad."""
import copy, importlib.machinery, importlib.util, json, os, subprocess, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone

BIN = os.path.join(os.path.dirname(__file__), "..", "bin")
HEAD = "a" * 40


def ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def load_ship():
    loader = importlib.machinery.SourceFileLoader("ship_gates", os.path.join(BIN, "ship"))
    spec = importlib.util.spec_from_loader("ship_gates", loader)
    S = importlib.util.module_from_spec(spec)
    loader.exec_module(S)
    S.STATE_DIR = tempfile.mkdtemp()
    return S


sys.path.insert(0, BIN)
import fleet_config  # noqa: E402

# the generic built-in policy (not the machine's config), as ship_policy shapes it
POLICY = dict(copy.deepcopy(fleet_config.DEFAULTS["ship"]), tier=None, human_paths=[])


def greptile_summary(sha, score, at):
    return {"url": "u-g", "author": {"login": "greptile-apps"}, "createdAt": at, "updatedAt": at,
            "body": f"greptile_confidence_score:{score} Last reviewed commit: x/commit/{sha}"}


def thread(level, replies=0, reply_at=None, resolved=False):
    first = {"author": {"login": "greptile-apps"}, "body": f'<img alt="{level}">'}
    return {"isResolved": resolved, "isOutdated": False,
            "comments": {"totalCount": 1 + replies, "nodes": [first]},
            "last": {"nodes": [{"createdAt": reply_at or ago(1), "author": {"login": "octocat"}}]}}


def check(status, conclusion, started=2):
    return {"__typename": "CheckRun", "name": "ci", "status": status, "conclusion": conclusion, "startedAt": ago(started),
            "detailsUrl": "", "checkSuite": {"workflowRun": {"databaseId": 1, "workflow": {"name": "CI"}}}}


def pr(checks=None, threads=(), comments=(), checks_more=False, threads_more=False, commit_at=None, created=None):
    green = [check("COMPLETED", "SUCCESS", started=20)]
    return {"number": 1, "url": "https://github.com/o/r/pull/1", "title": "t", "state": "OPEN", "isDraft": False,
            "merged": False, "mergedAt": None, "createdAt": created or ago(120), "mergeCommit": None,
            "headRefName": "b", "headRefOid": HEAD, "baseRefName": "main", "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN", "reviewDecision": None, "changedFiles": 1,
            "labels": {"nodes": []}, "files": {"nodes": [{"path": "src/x.ts", "changeType": "MODIFIED"}]},
            "latestOpinionatedReviews": {"nodes": []},
            "commits": {"nodes": [{"commit": {"committedDate": commit_at or ago(30), "statusCheckRollup": {
                "contexts": {"pageInfo": {"hasNextPage": checks_more}, "nodes": green if checks is None else checks}}}}]},
            "reviews": {"nodes": []},
            "reviewThreads": {"pageInfo": {"hasNextPage": threads_more}, "nodes": list(threads)},
            "comments": {"nodes": list(comments)}, "timelineItems": {"nodes": []}}


class Gates(unittest.TestCase):
    def setUp(self):
        self.S = load_ship()
        S = self.S
        S.load_policy = lambda r, *a: dict(POLICY, require_selfreview=False)
        S.lines_since = lambda *a: 0
        real = S.R.Greptile.installed
        S.R.Greptile.installed = lambda *a: True
        self.addCleanup(setattr, S.R.Greptile, "installed", real)
        S.reviewer_available = lambda *a: False
        S.required_checks = lambda *a: None
        S.pr_files = lambda o, r, n, p: [f["path"] for f in p["files"]["nodes"]]

    def ev(self, p):
        s = self.state(p)
        return s["state"], s["next"]

    def state(self, p):
        self.S.fetch = lambda o, r, n: p
        return self.S.evaluate("o", "r", 1)[0]

    def clean(self, **kw):
        return pr(comments=[greptile_summary(HEAD, 5, ago(10))], **kw)

    def test_clean_pr_awaits_human(self):
        self.assertEqual(self.ev(self.clean())[0], "awaiting-human")

    def test_draft_is_marked_ready_before_the_review_bot_is_asked(self):
        # Greptile skips drafts in some repos: asking it on a draft waits on silence
        s = self.state(dict(pr(), isDraft=True))
        self.assertEqual((s["state"], s["act"]), ("draft-clean", "ready"))

    def test_draft_is_marked_ready_while_ci_runs(self):
        s = self.state(dict(pr(checks=[check("IN_PROGRESS", None)]), isDraft=True))
        self.assertEqual((s["state"], s["act"]), ("draft-clean", "ready"))

    def test_draft_tagged_into_silence_is_marked_ready_not_handed_to_a_human(self):
        tag = {"url": "u-t", "author": {"login": "octocat"}, "createdAt": ago(90), "updatedAt": ago(90),
               "body": "@greptileai review"}
        s = self.state(dict(pr(comments=[tag]), isDraft=True))
        self.assertEqual((s["state"], s["act"]), ("draft-clean", "ready"))
        self.assertNotIn("stuck", s)
        self.assertEqual(self.ev(pr(comments=[tag]))[0], "needs-human", "a ready PR's silence still goes to a human")

    def test_draft_with_open_finding_shows_the_finding(self):
        p = dict(pr(threads=[thread("P1")], comments=[greptile_summary(HEAD, 3, ago(10))]), isDraft=True)
        self.assertEqual(self.ev(p)[0], "review-findings")

    def test_draft_needing_a_rereview_is_marked_ready(self):
        self.S.lines_since = lambda *a: 999
        p = dict(pr(comments=[greptile_summary("b" * 40, 5, ago(60))]), isDraft=True)
        s = self.state(p)
        self.assertEqual((s["state"], s["act"]), ("draft-clean", "ready"))
        self.assertEqual(self.ev(dict(p, isDraft=False))[0], "review-stale")

    def test_draft_with_red_ci_fixes_ci_first(self):
        self.assertEqual(self.ev(dict(pr(checks=[check("COMPLETED", "FAILURE")]), isDraft=True))[0], "ci-red")

    def test_draft_without_a_review_bot_waits_for_ci_before_ready(self):
        self.S.load_policy = lambda r, *a: dict(POLICY, require_selfreview=False, review="none")
        self.assertEqual(self.ev(dict(pr(checks=[check("IN_PROGRESS", None)]), isDraft=True))[0], "ci-running")

    def test_truncated_checks_fail_closed(self):
        self.assertEqual(self.ev(self.clean(checks_more=True))[0], "needs-human")

    def test_truncated_threads_fail_closed(self):
        self.assertEqual(self.ev(self.clean(threads_more=True))[0], "needs-human")

    def test_old_commit_just_pushed_is_not_green(self):
        # commit made 2 h ago, no checks yet, ship has never seen this head: CI hasn't registered
        p = pr(checks=[], comments=[greptile_summary(HEAD, 5, ago(10))], commit_at=ago(120))
        self.assertEqual(self.ev(p)[0], "ci-running")

    def test_grace_ends_once_head_seen_long_ago(self):
        S = self.S
        S.write_json(S.ledger_path("o/r", 1), {"head": HEAD, "head_seen_at": ago(30)})
        p = pr(checks=[], comments=[greptile_summary(HEAD, 5, ago(10))], commit_at=ago(120))
        self.assertNotEqual(self.ev(p)[0], "ci-running")

    def test_answered_p1_needs_rereview(self):
        # verdict 10 min ago, agent replied "won't fix" 2 min ago: a re-review must confirm
        p = self.clean(threads=[thread("P1", replies=1, reply_at=ago(2))])
        state, nxt = self.ev(p)
        self.assertEqual(state, "review-stale", nxt)

    def test_answered_p1_before_last_review_is_cleared(self):
        p = self.clean(threads=[thread("P1", replies=1, reply_at=ago(30))])
        self.assertEqual(self.ev(p)[0], "awaiting-human")

    def test_unanswered_p1_blocks(self):
        self.assertEqual(self.ev(self.clean(threads=[thread("P1")]))[0], "review-findings")

    def test_no_review_wording(self):
        S = self.S
        S.load_policy = lambda r, *a: dict(POLICY, review="none")
        state, nxt = self.ev(pr())
        self.assertEqual(state, "awaiting-human")
        self.assertIn("no review configured", nxt)

    def check(self, name, conclusion="SUCCESS", status="COMPLETED", required=None):
        return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
                "startedAt": ago(5), "detailsUrl": f"https://ci/{name}", "isRequired": required,
                "checkSuite": {"workflowRun": {"databaseId": 9, "workflow": {"name": "CI"}}}}

    def test_optional_red_check_does_not_block(self):
        self.S.required_checks = lambda *a: {"tests"}
        p = self.clean(checks=[self.check("tests"), self.check("Audit workflows", "FAILURE", required=False)])
        state, nxt = self.ev(p)
        self.assertEqual(state, "awaiting-human")
        self.assertIn("optional check(s) failing", nxt)
        self.assertIn("Audit workflows", nxt)

    def test_required_red_check_blocks(self):
        self.S.required_checks = lambda *a: {"tests"}
        p = self.clean(checks=[self.check("tests", "FAILURE"), self.check("lint")])
        self.assertEqual(self.ev(p)[0], "ci-red")

    def test_is_required_flag_counts_even_if_not_listed(self):
        self.S.required_checks = lambda *a: {"tests"}
        p = self.clean(checks=[self.check("tests"), self.check("e2e", "FAILURE", required=True)])
        self.assertEqual(self.ev(p)[0], "ci-red")

    def test_required_check_not_started_is_pending(self):
        self.S.required_checks = lambda *a: {"tests", "migration-safety"}
        state, _ = self.ev(self.clean(checks=[self.check("tests")]))
        self.assertEqual(state, "ci-running")

    def test_required_check_that_never_starts_goes_to_a_human(self):
        S = self.S
        S.required_checks = lambda *a: {"build-and-deploy"}
        S.write_json(S.ledger_path("o/r", 1), {"head": HEAD, "head_seen_at": ago(S.REQUIRED_START_MIN + 5)})
        state, nxt = self.ev(self.clean(checks=[]))
        self.assertEqual(state, "needs-human")
        self.assertIn("build-and-deploy", nxt)

    def test_unknown_or_no_required_checks_gate_everything(self):
        for req in (None, set()):
            self.S.required_checks = lambda *a, req=req: req
            p = self.clean(checks=[self.check("tests"), self.check("Audit workflows", "FAILURE")])
            self.assertEqual(self.ev(p)[0], "ci-red", req)

    def test_checks_all_policy_ignores_branch_protection(self):
        S = self.S
        S.required_checks = lambda *a: {"tests"}
        S.load_policy = lambda r, *a: dict(POLICY, require_selfreview=False, checks="all")
        p = self.clean(checks=[self.check("tests"), self.check("Audit workflows", "FAILURE")])
        self.assertEqual(self.ev(p)[0], "ci-red")

    def test_graphql_errors_die_cleanly(self):
        S = self.S
        S.gh_json = lambda *a: {"errors": [{"message": "Something went wrong"}]}
        with self.assertRaises(SystemExit):
            S.fetch("o", "r", 1)


class RetagLock(unittest.TestCase):
    def test_failed_comment_releases_lock(self):
        S = load_ship()
        S.ensure_label = lambda *a: None
        real = S.subprocess.run
        calls = []

        def fake(args, **kw):
            calls.append(args)
            return type("P", (), {"returncode": 1, "stderr": "boom", "stdout": ""})()
        S.subprocess.run = fake
        s = {"repo": "o/r", "number": 1, "head": HEAD, "act": "retag", "labels": []}
        try:
            pol = {"review": "greptile"}
            self.assertIn("failed", S.act(s, {}, pol))
            self.assertIn("failed", S.act(s, {}, pol), "second try must not say 'already requested'")
        finally:
            S.subprocess.run = real
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
