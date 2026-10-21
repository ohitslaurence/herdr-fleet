"""fleet_events and what writes to it: PR state changes from ship (with the failed run's log
fetched once), so a reader can ask what changed and why."""
import json, os, subprocess, sys, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bin"))
sys.path.insert(0, os.path.join(HERE, "golden"))
import fleet_events as E  # noqa: E402
from facts import load_ship  # noqa: E402


class Events(unittest.TestCase):
    def test_since_reads_across_rotation_and_redacts(self):
        d = tempfile.mkdtemp()
        E.append(d, "pr", where="api#1", now="ci-red", reason="token ghp_" + "a" * 36)
        with open(E.path(d)) as f:
            first = f.read()
        self.assertNotIn("ghp_" + "a" * 36, first)
        with mock.patch.object(E, "MAX_BYTES", 1):
            E.append(d, "pr", where="api#2", now="mergeable")
        got = E.since(d, "2000-01-01T00:00:00Z")
        self.assertEqual([e["where"] for e in got], ["api#1", "api#2"])
        self.assertEqual(E.since(d, "2999-01-01T00:00:00Z"), [])

    def test_a_failed_append_is_reported_not_raised(self):
        import io, contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            E.append("/proc/nope", "pr", where="x")
        self.assertIn("fleet events: not recorded", err.getvalue())
        self.assertEqual(E.since("/proc/nope", "2000-01-01T00:00:00Z"), [])


class ShipRecords(unittest.TestCase):
    def setUp(self):
        self.S = load_ship()
        self.calls = []

        def fake(args, **kw):
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 0, "build\tstep\t2026-10-20T05:00:00.1234567Z \x1b[31mFAIL\x1b[0m x\n", "")
        for target, attr, value in ((self.S.proc, "run", fake), (self.S, "on_branch", lambda b: False),
                                    (self.S, "author_kind", lambda: None)):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def state(self, state, **kw):
        return dict({"repo": "o/api", "number": 1, "url": "https://github.com/o/api/pull/1", "title": "t",
                     "branch": "b", "state": state}, **kw)

    def test_state_changes_become_events_and_failures_carry_their_log_once(self):
        red = self.state("ci-red", failing=["CI (https://github.com/o/api/actions/runs/42/job/7)"])
        self.S.persist(self.state("ci-running"))
        self.S.persist(red)
        self.S.persist(red)  # same run again: no second fetch, no second event
        events = E.since(self.S.STATE_DIR, "2000-01-01T00:00:00Z")
        self.assertEqual([(e["was"], e["now"]) for e in events], [(None, "ci-running"), ("ci-running", "ci-red")])
        fetches = [c for c in self.calls if c[:3] == ["gh", "run", "view"]]
        self.assertEqual(len(fetches), 1)
        with open(self.S.ledger_path("o/api", 1)) as f:
            log = json.load(f)["failure_log"]
        self.assertEqual((log["run"], log["tail"]), ("42", "FAIL x"))


if __name__ == "__main__":
    unittest.main()
