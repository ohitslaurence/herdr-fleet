"""fleet learn: reads only verdicts, summarises what agents fixed and dismissed, and proposes
without applying anything or sending more than the findings."""
import json, os, subprocess, sys, tempfile, time, unittest
from datetime import datetime, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bin"))
import fleet_learn as L  # noqa: E402

SENT = {"repo", "pr", "id", "lens", "severity", "file", "summary", "generalises", "verdict", "why"}


def finding(i, lens, verdict, rule="", why=""):
    return {"id": f"c{i}", "lens": lens, "severity": "suggestion", "file": "x.py", "line": 1,
            "summary": f"s{i}", "detail": "LONG-DETAIL-TEXT", "generalises": rule, "verdict": verdict,
            "why": why, "decided_head": "HEAD-SHA-XYZ"}


def iso(days_ago):
    return datetime.fromtimestamp(time.time() - days_ago * 86400, timezone.utc).isoformat(timespec="seconds")


def read(path):
    with open(path) as f:
        return f.read()


class Learn(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "reviews"))
        for k, v in (("STATE_DIR", self.d), ("OUT_DIR", os.path.join(self.d, "learn"))):
            p = mock.patch.object(L, k, v)
            p.start()
            self.addCleanup(p.stop)
        self.write("o__api__1", [{"head": "a", "kind": "claude", "at": iso(1), "findings": [
            finding(1, "test", "fixed", "lint: no assert-free tests"), finding(2, "craft", "dismissed", why="intended"),
            finding(3, "arch", None)]}, {"head": "b", "kind": "claude", "failed": "no END", "findings": []}])
        self.write("o__web__7", [{"head": "c", "kind": "codex", "at": iso(40), "findings": [
            finding(1, "test", "fixed", "lint: no assert-free tests")]},
            {"head": "d", "kind": "codex", "findings": [finding(2, "clarity", "fixed")]}])  # no `at`

    def write(self, stem, reviews):
        with open(os.path.join(self.d, "reviews", stem + ".json"), "w") as f:
            json.dump({"reviews": reviews}, f)

    def snapshot(self):
        root = os.path.join(self.d, "reviews")
        return {n: read(os.path.join(root, n)) for n in os.listdir(root)}

    def test_only_verdicts_count(self):
        got = sorted((x["repo"], x["pr"], x["id"]) for x in L.findings())
        self.assertEqual(got, [("o/api", 1, "c1"), ("o/api", 1, "c2"), ("o/web", 7, "c1"), ("o/web", 7, "c2")])
        self.assertEqual(sorted(x["id"] for x in L.findings(repo="o/web")), ["c1", "c2"])

    def test_days_keeps_recent_and_drops_undated(self):
        self.assertEqual([(x["repo"], x["id"]) for x in L.findings(days=30)], [("o/api", "c1"), ("o/api", "c2")])

    def test_summary(self):
        s = L.summary(L.findings())
        self.assertEqual((s["findings"], s["prs"]), (4, 2))
        self.assertEqual(s["by_lens"]["test"], {"fixed": 2})
        self.assertEqual(s["rule_ideas"], [{"rule": "lint: no assert-free tests", "times": 2}])
        self.assertEqual(s["dismissed"][0]["why"], "intended")

    def test_propose_sends_only_the_findings_and_applies_nothing(self):
        seen = {}

        def fake(args, **kw):
            seen.update(args=args, input=kw["input"])
            return subprocess.CompletedProcess(args, 0, "1. add a rule\n", "")
        before = self.snapshot()
        with mock.patch.object(L.fleet_proc, "run", fake):
            path, _ = L.propose(L.findings(), None)
        self.assertEqual(seen["args"][seen["args"].index("--tools") + 1], "")  # no tools: it can only answer
        sent = [json.loads(line) for line in seen["input"].splitlines() if line.startswith("{")]
        self.assertEqual(len(sent), 4)
        self.assertTrue(all(set(x) == SENT for x in sent))
        self.assertNotIn("LONG-DETAIL-TEXT", seen["input"])
        self.assertNotIn("HEAD-SHA-XYZ", seen["input"])
        self.assertTrue(read(path).endswith("1. add a rule\n"))
        self.assertEqual(self.snapshot(), before)

    def test_cli_rejects_typos_and_reports_failures(self):
        self.assertEqual(L.main(["--porpose"]), 2)
        self.assertEqual(L.main(["--days", "x"]), 2)

        def gone(args, **kw):
            raise FileNotFoundError(2, "No such file or directory", "claude")
        with mock.patch.object(L.fleet_proc, "run", gone):
            self.assertEqual(L.main(["--propose"]), 1)

    def test_corrupt_review_file_is_an_error_not_silence(self):
        with open(os.path.join(self.d, "reviews", "o__bad__2.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(L.main([]), 1)


if __name__ == "__main__":
    unittest.main()
