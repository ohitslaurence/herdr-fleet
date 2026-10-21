"""The quiet craft review: findings parsed from the reviewer, kept off the PR, gating the draft
until each has a verdict, and pruned to what learning needs."""
import glob, json, os, subprocess, sys, tempfile, time, unittest
from datetime import datetime
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bin"))
sys.path.insert(0, os.path.join(HERE, "golden"))
import fleet_craft as FR  # noqa: E402
from facts import load_ship  # noqa: E402
from test_golden import FIXTURES, Lenient, load, tighten_policy  # noqa: E402

S = load_ship()
REVIEWER = """Some preamble the brief said not to write.
{"lens": "test", "severity": "concern", "file": "tests/x.py", "line": 9, "summary": "asserts nothing", "detail": "d", "generalises": "lint: no assert-free tests"}
not json {
{"lens": "weird", "severity": "huge", "summary": "odd values get defaults"}
{"lens": "craft", "summary": ""}
END"""


class Parse(unittest.TestCase):
    def test_findings_and_defaults(self):
        got = FR.parse(REVIEWER)
        self.assertEqual([f["id"] for f in got], ["c1", "c2"])
        self.assertEqual((got[0]["lens"], got[0]["severity"], got[0]["line"]), ("test", "concern", 9))
        self.assertEqual((got[1]["lens"], got[1]["severity"], got[1]["line"]), ("craft", "suggestion", None))

    def test_no_end_is_a_failed_run_not_clean(self):
        self.assertIsNone(FR.parse('{"summary": "cut off'))
        self.assertEqual(FR.parse("END"), [])

    def test_caps_padding(self):
        text = "\n".join(json.dumps({"summary": f"s{i}"}) for i in range(50)) + "\nEND"
        self.assertEqual(len(FR.parse(text)), FR.MAX_FINDINGS)


class Store(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_verdicts_go_on_the_latest_review(self):
        FR.add(self.d, "o/r", 1, "a" * 40, "codex", FR.parse(REVIEWER))
        self.assertEqual(FR.decide(self.d, "o/r", 1, ["c1", "c9"], "fixed", "added the assert", "b" * 40), ["c9"])
        last, failed = FR.latest(FR.load(self.d, "o/r", 1))
        self.assertEqual(([f["id"] for f in FR.undecided(last)], failed), (["c2"], 0))
        self.assertEqual((last["findings"][0]["verdict"], last["findings"][0]["decided_head"]), ("fixed", "b" * 40))

    def test_failed_runs_are_counted_not_reviewed(self):
        FR.add(self.d, "o/r", 1, "a" * 40, "codex", [], failed="no END line")
        FR.add(self.d, "o/r", 1, "a" * 40, "codex", [], failed="no END line")
        self.assertEqual(FR.latest(FR.load(self.d, "o/r", 1)), (None, 2))

    def test_prune_keeps_what_learning_needs(self):
        FR.add(self.d, "o/r", 1, "a" * 40, "codex", FR.parse(REVIEWER))
        FR.decide(self.d, "o/r", 1, ["c1"], "fixed", "why")
        self.assertEqual(FR.prune(self.d, 14), 0)  # fresh: untouched
        self.assertEqual(FR.prune(self.d, 14, now=time.time() + 15 * 86400), 1)
        f = FR.load(self.d, "o/r", 1)["reviews"][0]["findings"][0]
        self.assertNotIn("detail", f)
        self.assertEqual((f["summary"], f["verdict"], f["generalises"]), ("asserts nothing", "fixed",
                                                                           "lint: no assert-free tests"))

    def test_store_is_private(self):
        FR.add(self.d, "o/r", 1, "a" * 40, "codex", [])
        self.assertEqual(os.stat(os.path.join(self.d, "reviews")).st_mode & 0o077, 0)


def decide(fx, craft, **policy):
    fx = tighten_policy(fx, **{"craft_review": True, **policy})
    fx["calls"]["craft"] = craft
    s, _, _ = S.decide(Lenient(fx), datetime.fromisoformat(fx["now"]))
    return s


class Gate(unittest.TestCase):
    fx = load(next(p for p in FIXTURES if "draft-clean" in p))

    def review(self, *verdicts, failed=None):
        r = {"head": "x", "kind": "codex", "findings": [{"id": f"c{i + 1}", "verdict": v} for i, v in enumerate(verdicts)]}
        if failed:
            r["failed"] = failed
        return r

    def test_no_review_yet_runs_one(self):
        s = decide(self.fx, {"reviews": []})
        self.assertEqual((s["state"], s["act"]), ("craft-review", "craft-review"))

    def test_open_findings_hold_the_draft(self):
        s = decide(self.fx, {"reviews": [self.review("fixed", None)]})
        self.assertEqual(s["state"], "craft-findings")
        self.assertIn("1 craft finding", s["next"])

    def test_all_decided_moves_on(self):
        s = decide(self.fx, {"reviews": [self.review("fixed", "dismissed")]})
        self.assertEqual(s["state"], "draft-clean")
        self.assertEqual(s["craft"]["undecided"], 0)

    def test_steps_aside_after_two_failures(self):
        failed = {"reviews": [self.review(failed="x"), self.review(failed="x")]}
        s = decide(self.fx, failed)
        self.assertEqual(s["state"], "draft-clean")
        self.assertEqual(s["craft"], {"skipped": "the reviewer failed twice"})

    def test_off_ignores_open_findings(self):
        self.assertEqual(decide(self.fx, {"reviews": [self.review(None)]}, craft_review=False)["state"], "draft-clean")

    def test_ready_prs_are_not_held(self):
        """Turning it on must not park PRs that are already out of draft and under review."""
        fx = load(next(p for p in FIXTURES if "awaiting-human" in p))
        self.assertFalse(fx["pr"]["isDraft"])
        self.assertEqual(decide(fx, {"reviews": []})["state"], "awaiting-human")

    def test_auto_merge_holds_ready_prs_for_it(self):
        """With merge = "auto" it's the only review: a PR opened ready must not merge without it."""
        fx = load(next(p for p in FIXTURES if "awaiting-human" in p))
        self.assertFalse(fx["pr"]["isDraft"])
        self.assertEqual(decide(fx, {"reviews": []}, merge="auto")["state"], "craft-review")
        self.assertEqual(decide(fx, {"reviews": [self.review(None)]}, merge="auto")["state"], "craft-findings")
        self.assertEqual(decide(fx, {"reviews": [self.review("fixed")]}, merge="auto")["state"],
                         decide(fx, {"reviews": []}, merge="auto", craft_review=False)["state"])

    def test_brief_states_the_parser_cap(self):
        brief = Path(HERE, "..", "skills", "ship", "craft-brief.md").read_text()
        self.assertIn("at most {{MAX_FINDINGS}}", brief)


class Headless(unittest.TestCase):
    def test_claude_prompt_is_not_swallowed_by_allowed_tools(self):
        """`--allowedTools` is variadic: a prompt after it became a tool name and claude got no
        prompt at all, so every claude review came back empty."""
        seen = {}

        def fake(args, **kw):
            seen.update(args=args, input=kw.get("input"))
            return subprocess.CompletedProcess(args, 0, "END\n", "")
        real, S.subprocess.run = S.subprocess.run, fake
        try:
            self.assertEqual(S.headless("claude", "-", "-", "PROMPT", "/tmp", "/tmp"), "END\n")
        finally:
            S.subprocess.run = real
        self.assertNotIn("PROMPT", seen["args"])
        self.assertEqual(seen["input"], "PROMPT")


class Cli(unittest.TestCase):
    def test_verdict_needs_a_reason(self):
        bin_ship = os.path.join(HERE, "..", "bin", "ship")
        home = tempfile.mkdtemp()
        p = subprocess.run([bin_ship, "findings", "https://github.com/o/r/pull/1", "c1", "fixed"],
                           capture_output=True, text=True, env=dict(os.environ, HOME=home), cwd=home, timeout=30)
        self.assertEqual(p.returncode, 1)
        self.assertIn("<why", p.stderr)


if __name__ == "__main__":
    unittest.main()
