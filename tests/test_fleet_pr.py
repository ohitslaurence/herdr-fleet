"""fleet_pr: only PRs in the fleet's rows are fetched; the answer is capped, redacted and cached."""
import json, os, subprocess, sys, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bin"))
import fleet_pr as P  # noqa: E402

ROWS = [{"kind": "pane", "where": "api#1"},
        {"kind": "pr", "section": "BACKLOG", "where": "old#3", "url": "https://github.com/o/old/pull/3", "repo": "o/old",
         "number": 3, "head": "x"},
        {"kind": "pr", "where": "api#1", "url": "https://github.com/o/api/pull/1", "repo": "o/api", "number": 1,
         "head": "abc"}]
VIEW = {"title": "Fix", "body": "token ghp_" + "a" * 36, "state": "OPEN", "isDraft": False, "headRefOid": "abc",
        "headRefName": "fix", "baseRefName": "main",
        "files": [{"path": f"f{i}", "additions": 1, "deletions": 0} for i in range(P.MAX_FILES + 5)],
        "reviews": [{"author": {"login": "greptile"}, "state": "COMMENTED", "body": "looks ok",
                     "submittedAt": "2026-10-20T02:00:00Z"},
                    {"author": {"login": "x"}, "state": "COMMENTED", "body": "", "submittedAt": "2026-10-20T02:00:01Z"}],
        "comments": [{"author": {"login": "ada"}, "body": "y" * 5000, "createdAt": "2026-10-20T01:00:00Z"}],
        "statusCheckRollup": [{"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "FAILURE",
                               "detailsUrl": "https://x/1"},
                              {"__typename": "StatusContext", "context": "deploy", "state": "PENDING", "targetUrl": None}]}
DIFF = "+x = 1\n" * (P.DIFF_CHARS // 7 + 10)
INLINE = [{"user": {"login": "greptile"}, "body": "nit", "path": "a.py", "line": 3, "created_at": "2026-10-20T03:00:00Z"}]


def fake_gh(calls, fail=()):
    """gh that answers from the fixtures; subcommands in fail ("view", "diff", "api") exit 1."""
    def run(args, timeout=None, **kw):
        calls.append(args)
        sub = args[2] if args[1] == "pr" else "api"
        if sub in fail:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        out = {"view": json.dumps(VIEW), "diff": DIFF, "api": json.dumps(INLINE)}[sub]
        return subprocess.CompletedProcess(args, 0, out, "")
    return run


class FleetPr(unittest.TestCase):
    def setUp(self):
        P._cache.clear()
        P._fetches.clear()
        self.calls = []
        p = mock.patch.object(P.fleet_proc, "run", fake_gh(self.calls))
        p.start()
        self.addCleanup(p.stop)

    def refused(self, *args, **kw):
        with self.assertRaises(P.Refused) as cm:
            P.detail(*args, **kw)
        return cm.exception

    def test_only_live_fleet_prs_are_fetched(self):
        self.assertIn("isn't a PR in the fleet", str(self.refused(ROWS, "other#2")))
        self.refused(ROWS[:1], "api#1")  # a pane named like a PR isn't one
        self.refused(ROWS, "old#3")  # the backlog fleet_overview doesn't show
        self.assertEqual(self.calls, [])

    def test_a_short_ref_two_repos_share_is_refused(self):
        fork = dict(ROWS[2], repo="evil/api", url="https://github.com/evil/api/pull/1")
        why = self.refused(ROWS + [fork], "api#1")
        self.assertEqual(why.code, "ambiguous")
        self.assertIn("matches 2 PRs", str(why))
        self.assertIn("evil/api#1", str(why))  # names both, so the caller can pick
        self.assertEqual(P.detail(ROWS + [fork], "https://github.com/o/api/pull/1")["repo"], "o/api")
        self.assertEqual(P.detail(ROWS + [fork], "evil/api#1", now=0)["repo"], "evil/api")

    def test_by_where_owner_repo_or_url(self):
        self.assertEqual(P.detail(ROWS, "api#1")["title"], "Fix")
        self.assertEqual(P.detail(ROWS, "o/api#1", now=0)["title"], "Fix")
        self.assertEqual(P.detail(ROWS, "https://github.com/o/api/pull/1", now=0)["title"], "Fix")

    def test_secrets_are_redacted_before_text_is_cut(self):
        VIEW["comments"].append({"author": {"login": "b"}, "createdAt": "2026-10-20T04:00:00Z",
                                 "body": "x" * (P.COMMENT_CHARS - 20) + " ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3"})
        self.addCleanup(VIEW["comments"].pop)
        last = P.detail(ROWS, "api#1")["comments"][-1]["body"]
        self.assertNotIn("ghp_", last)
        self.assertNotIn("a1B2c3D4", last)

    def test_answer_is_capped_and_redacted(self):
        got = P.detail(ROWS, "api#1")
        self.assertEqual(len(got["diff"]), P.DIFF_CHARS)
        self.assertEqual(got["diff_chars"], len(DIFF))
        self.assertEqual((len(got["files"]), got["files_count"]), (P.MAX_FILES, P.MAX_FILES + 5))
        self.assertNotIn("ghp_", got["body"])
        self.assertEqual([c["kind"] for c in got["comments"]], ["comment", "review", "inline"])  # oldest first
        self.assertEqual(len(got["comments"][0]["body"]), P.COMMENT_CHARS)
        self.assertEqual(got["comments"][2]["path"], "a.py")
        self.assertEqual(got["checks"], [{"name": "test", "state": "FAILURE", "url": "https://x/1"},
                                         {"name": "deploy", "state": "PENDING", "url": None}])

    def test_cached_per_head_until_it_expires(self):
        P.detail(ROWS, "api#1", now=1000)
        P.detail(ROWS, "api#1", now=1000 + P.CACHE_S - 1)
        self.assertEqual(len(self.calls), 3)
        P.detail(ROWS, "api#1", now=1000 + P.CACHE_S)
        self.assertEqual(len(self.calls), 6)
        moved = [dict(ROWS[2], head="def")]
        P.detail(moved, "api#1", now=1000 + P.CACHE_S)
        self.assertEqual(len(self.calls), 9)

    def test_concurrent_callers_share_one_fetch(self):
        import threading
        release = threading.Event()
        slow = fake_gh(self.calls)

        def run(args, **kw):
            release.wait(5)
            return slow(args, **kw)
        with mock.patch.object(P.fleet_proc, "run", run):
            got = []
            threads = [threading.Thread(target=lambda: got.append(P.detail(ROWS, "api#1"))) for _ in range(4)]
            for t in threads:
                t.start()
            release.set()
            for t in threads:
                t.join(5)
        self.assertEqual((len(got), len(self.calls)), (4, 3))

    def test_the_hourly_budget_caps_github_calls(self):
        budget = P.FETCHES_PER_HOUR
        rows = [dict(ROWS[2], where=f"api#{n}", number=n, url=None) for n in range(1, budget + 3)]
        for n in range(1, budget + 1):
            P.detail(rows, f"api#{n}", now=100)
        self.assertEqual(self.refused(rows, f"api#{budget + 1}", now=200).code, "budget")
        self.assertEqual(P.detail(rows, "api#1", now=200)["title"], "Fix")  # cached ones still answer
        P.detail(rows, f"api#{budget + 2}", now=100 + 3601)
        self.assertEqual(len(self.calls), 3 * (P.FETCHES_PER_HOUR + 1))

    def failing(self, *subs):
        return mock.patch.object(P.fleet_proc, "run", fake_gh(self.calls, fail=subs))

    def test_a_failed_diff_is_none(self):
        with self.failing("diff"):
            got = P.detail(ROWS, "api#1")
        self.assertEqual((got["diff"], got["diff_chars"], got["title"]), (None, None, "Fix"))

    def test_failed_inline_comments_leave_the_rest(self):
        with self.failing("api"):
            got = P.detail(ROWS, "api#1")
        self.assertEqual([c["kind"] for c in got["comments"]], ["comment", "review"])

    def test_an_unexpected_answer_is_a_cached_failure_not_a_stuck_gate(self):
        def odd(args, timeout=None, **kw):
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 0, "[]", "")  # a list where an object belongs
        with mock.patch.object(P.fleet_proc, "run", odd):
            self.assertEqual(self.refused(ROWS, "api#1", now=0).code, "upstream")
            self.refused(ROWS, "api#1", now=1)
        self.assertEqual((len(self.calls), P._gates, len(P._fetches)), (3, {}, 1))

    def test_statuses_say_whose_problem_it_is(self):
        self.assertEqual(self.refused(ROWS, "nope#1").code, "not_found")
        P._fetches.extend([0] * P.FETCHES_PER_HOUR)
        self.assertEqual(self.refused(ROWS, "api#1", now=1).code, "budget")
        self.assertEqual(P._gates, {})  # a refused call leaves no gate behind
        P._fetches.clear()
        with self.failing("view"):
            self.refused(ROWS, "api#1", now=2)
            self.refused(ROWS, "api#1", now=3)  # the cached refusal, raised again
        self.assertEqual(P._gates, {})

    def test_a_failed_view_is_an_error_cached_briefly(self):
        with self.failing("view"):
            self.assertIn("didn't answer", str(self.refused(ROWS, "api#1", now=0)))
            self.refused(ROWS, "api#1", now=P.FAILED_CACHE_S - 1)
            self.assertEqual(len(self.calls), 3)
            self.refused(ROWS, "api#1", now=P.FAILED_CACHE_S)
            self.assertEqual(len(self.calls), 6)

    def test_gh_answers_within_the_remote_deadline(self):
        import fleet_remote
        self.assertLess(P.GH_TIMEOUT_S, fleet_remote.TIMEOUT_S - 1)  # tool_pr waits TIMEOUT_S - 1

if __name__ == "__main__":
    unittest.main()
