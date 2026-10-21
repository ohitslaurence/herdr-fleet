"""ship's decide() against anonymised recordings of real PRs (tests/golden/fixtures, made by
tests/golden/record.py), plus monotonicity: making policy or the PR stricter never makes a PR
more mergeable."""
import copy, glob, json, os, sys, unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "golden"))
from facts import RecordedFacts, load_ship  # noqa: E402

S = load_ship()
FIXTURES = sorted(glob.glob(os.path.join(HERE, "golden", "fixtures", "*.json")))
READY = {"mergeable", "awaiting-human", "draft-clean", "behind"}  # nothing left for the agent to fix


def load(path):
    with open(path) as f:
        return json.load(f)


def decide(fx, facts_cls=RecordedFacts):
    s, _, _ = S.decide(facts_cls(fx), datetime.fromisoformat(fx["now"]))
    return s


class Lenient(RecordedFacts):
    """For mutated fixtures, decide() may ask what the recording never needed. Unknowns get the
    cautious answer: no review budget saved by small diffs, no reviewer, nothing posted."""
    DEFAULTS = {"posted": [], "free_tries": [], "reviewer_available": False, "required": None,
                "craft": {"reviews": []}}

    def _get(self, key):
        if key in self.calls:
            return self.calls[key]
        if key.startswith("lines_since:"):
            return None
        if key.startswith("requests:"):
            return 0
        if key.startswith("installed:"):
            return True
        return self.DEFAULTS[key]


class Golden(unittest.TestCase):
    def test_fixtures_exist(self):
        self.assertGreaterEqual(len(FIXTURES), 20)

    def test_verdicts(self):
        for path in FIXTURES:
            fx = load(path)
            with self.subTest(os.path.basename(path)):
                s = decide(fx)
                self.assertEqual({k: s.get(k) for k in fx["expected"]}, fx["expected"])


def tighten_policy(fx, **kw):
    fx = copy.deepcopy(fx)
    fx["policy"].update(kw)
    return fx


def stricter_scores(fx):
    fx = copy.deepcopy(fx)
    for t in fx["policy"]["tiers"].values():
        t["min_score"] = 5
    return fx


def add_failing_required_check(fx):
    fx = copy.deepcopy(fx)
    nodes = fx["pr"]["commits"]["nodes"]
    if not nodes:
        return None
    roll = nodes[0]["commit"].setdefault("statusCheckRollup", None) or {"contexts": {"nodes": []}}
    nodes[0]["commit"]["statusCheckRollup"] = roll
    roll.setdefault("contexts", {"nodes": []})["nodes"].append({
        "__typename": "CheckRun", "name": "added-required", "status": "COMPLETED", "conclusion": "FAILURE",
        "detailsUrl": "https://example.invalid/x", "startedAt": fx["now"], "isRequired": True,
        "checkSuite": {"workflowRun": {"databaseId": 1, "workflow": {"name": "added"}}}})
    if fx["calls"].get("required") is not None:
        fx["calls"]["required"] = fx["calls"]["required"] + ["added-required"]
    return fx


def add_open_p1(fx):
    fx = copy.deepcopy(fx)
    fx["pr"]["reviewThreads"]["nodes"].append({
        "isResolved": False, "isOutdated": False,
        "comments": {"totalCount": 1, "nodes": [{"author": {"login": "greptile-apps"}, "body": 'alt="P1"'}]},
        "last": {"nodes": [{"createdAt": fx["now"], "author": {"login": "greptile-apps"}}]}})
    return fx


def add_open_craft_finding(fx):
    fx = tighten_policy(fx, craft_review=True)
    fx["calls"]["craft"] = {"reviews": [{"head": fx["pr"]["headRefOid"], "kind": "codex",
                                         "findings": [{"id": "c1", "verdict": None}]}]}
    return fx


TIGHTENINGS = {
    "every file protected": lambda fx: tighten_policy(fx, human_paths=["*"]),
    "min_score 5": stricter_scores,
    "self-review required": lambda fx: tighten_policy(fx, require_selfreview=True),
    "merge ask": lambda fx: tighten_policy(fx, merge="ask"),
    "failing required check": add_failing_required_check,
    "open P1 finding": add_open_p1,
    "craft review on": lambda fx: tighten_policy(fx, craft_review=True),
    "open craft finding": add_open_craft_finding,
}


class Monotonic(unittest.TestCase):
    def test_tightening_never_makes_a_pr_more_mergeable(self):
        for path in FIXTURES:
            fx = load(path)
            base = decide(fx, Lenient)["state"]
            for name, tighten in TIGHTENINGS.items():
                t = tighten(fx)
                if t is None:
                    continue
                with self.subTest(fixture=os.path.basename(path), tightening=name):
                    got = decide(t, Lenient)["state"]
                    if base not in READY:
                        self.assertNotIn(got, READY, f"{base} -> {got}")
                    if base != "mergeable":
                        self.assertNotEqual(got, "mergeable")

    def test_new_blockers_block_open_prs(self):
        for path in FIXTURES:
            fx = load(path)
            if fx["pr"]["state"] != "OPEN" or decide(fx, Lenient)["state"] not in READY:
                continue
            # (an open craft finding holds drafts only: test_craft_review.Gate)
            for name in ("failing required check", "open P1 finding"):
                with self.subTest(fixture=os.path.basename(path), blocker=name):
                    self.assertNotIn(decide(TIGHTENINGS[name](fx), Lenient)["state"], READY)


if __name__ == "__main__":
    unittest.main()
