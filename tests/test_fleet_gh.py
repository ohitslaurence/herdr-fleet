"""Batched PR lookups (fleet_gh), branch reads without git processes (fleet_paths.head_branch),
and the cockpit refresh only running ship where something may have changed."""
import importlib.machinery, importlib.util, json, os, subprocess, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
sys.path.insert(0, BIN)
import fleet_gh as G  # noqa: E402
import fleet_paths as P  # noqa: E402


def gql(data, rc=0):
    """A fake `gh api graphql` result."""
    return subprocess.CompletedProcess([], rc, stdout=json.dumps({"data": data}), stderr="")


class Batching(unittest.TestCase):
    def test_one_call_per_chunk_and_unknown_vs_none(self):
        pairs = [("acme/api", f"b{i}") for i in range(G.CHUNK + 5)]
        calls = []

        def fake(args, **kw):
            calls.append(args)
            q = args[-1]
            n = q.count("pullRequests(")
            # alias 0: a PR; alias 1: no PR; alias 2 missing (unknown); others: no PR
            data = {f"b{i}": {"pullRequests": {"nodes": []}} for i in range(n) if i != 2}
            data["b0"] = {"pullRequests": {"nodes": [{"number": 7, "state": "MERGED", "url": "u7"}]}}
            return gql(data)
        with mock.patch.object(G.subprocess, "run", fake):
            got = G.prs_for_branches(pairs)
        self.assertEqual(len(calls), 2)
        self.assertEqual(got[("acme/api", "b0")]["number"], 7)
        self.assertIsNone(got[("acme/api", "b1")])
        self.assertNotIn(("acme/api", "b10"), got)  # alias 2 of the sorted first chunk: unknown

    def test_failed_request_is_unknown(self):
        with mock.patch.object(G.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, "", "boom")):
            self.assertEqual(G.prs_for_branches([("acme/api", "x")]), {})
            self.assertEqual(G.pr_signatures(["https://github.com/acme/api/pull/1"]), {})

    def test_names_are_quoted(self):
        seen = []
        with mock.patch.object(G.subprocess, "run", lambda args, **k: seen.append(args[-1]) or gql({})):
            G.prs_for_branches([("acme/api", 'x"){evil}')])
        self.assertIn(json.dumps('x"){evil}'), seen[0])

    def test_signature_changes_with_ci(self):
        def sig(rollup):
            pr = {"state": "OPEN", "headRefOid": "a", "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": rollup}}}]}}
            with mock.patch.object(G.subprocess, "run", lambda *a, **k: gql({"p0": {"pullRequest": pr}})):
                return G.pr_signatures(["https://github.com/acme/api/pull/1"])["https://github.com/acme/api/pull/1"]
        self.assertNotEqual(sig("PENDING"), sig("SUCCESS"))


class HeadBranch(unittest.TestCase):
    def git(self, *a):
        subprocess.run(["git", *a], check=True, capture_output=True)

    def test_clone_worktree_subdir_detached(self):
        root = tempfile.mkdtemp()
        repo = os.path.join(root, "r")
        self.git("init", "-q", "-b", "main", repo)
        self.git("-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
        os.makedirs(os.path.join(repo, "src", "deep"))
        self.assertEqual(P.head_branch(os.path.join(repo, "src", "deep")), "main")
        wt = os.path.join(root, "r-wt-fix")
        self.git("-C", repo, "worktree", "add", "-q", wt, "-b", "r__fix/sub")
        self.assertEqual(P.head_branch(wt), "r__fix/sub")
        self.git("-C", wt, "checkout", "-q", "--detach")
        self.assertEqual(P.head_branch(wt), "HEAD")
        self.assertIsNone(P.head_branch(tempfile.mkdtemp()))


def load_cockpit():
    loader = importlib.machinery.SourceFileLoader("cockpit", os.path.join(BIN, "fleet-cockpit"))
    spec = importlib.util.spec_from_loader("cockpit", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def iso(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


class Refresh(unittest.TestCase):
    URLS = {s: f"https://github.com/acme/api/pull/{n}" for n, s in enumerate(
        ["quiet", "moved", "waiting", "old", "deploying", "new"], 1)}

    def test_ship_runs_only_where_something_may_have_changed(self):
        C = load_cockpit()
        tmp = tempfile.mkdtemp()
        C.LEDGER, C.SIGS, C.CACHE = os.path.join(tmp, "ledger"), os.path.join(tmp, "sigs.json"), os.path.join(tmp, "c.json")
        os.makedirs(C.LEDGER)
        u = self.URLS
        ledger = {"quiet": ("awaiting-human", 5), "moved": ("awaiting-human", 5), "waiting": ("ci-running", 5),
                  "old": ("deployed", C.RECHECK_MIN + 5), "deploying": ("deploying", 5)}
        for k, (state, ago) in ledger.items():
            with open(os.path.join(C.LEDGER, f"{k}.json"), "w") as f:
                json.dump({"url": u[k], "state": state, "checked_at": iso(ago)}, f)
        with open(C.SIGS, "w") as f:
            json.dump({u[k]: "s1" for k in ledger}, f)
        sigs = {u[k]: "s1" for k in ledger} | {u["moved"]: "s2", u["new"]: "s1"}
        ran = []

        def run(*args, timeout=60):
            if args[:3] == ("gh", "search", "prs"):
                return json.dumps([{"url": u[k]} for k in ("quiet", "moved", "waiting", "old", "new")])
            if args[0] == "ship":
                ran.append(args[1])
                return "{}"
            return None
        C.run, C.panes, C.collect, C.score_urgency = run, lambda: [], lambda: {"rows": []}, lambda rows: None
        with mock.patch.object(G, "pr_signatures", lambda urls: {x: sigs[x] for x in urls}), \
                mock.patch.object(G, "prs_for_branches", lambda pairs: {}):
            C.refresh(quiet=True)
        self.assertEqual(sorted(ran), sorted(u[k] for k in ("moved", "waiting", "old", "deploying", "new")))
        self.assertEqual(C.jload(C.SIGS, {})[u["moved"]], "s2")
        self.assertEqual(sorted(C.jload(C.CACHE, {})["urls"]), sorted(u.values()))


if __name__ == "__main__":
    unittest.main()
