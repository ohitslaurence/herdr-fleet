"""ship's after_deploy: a command from your own config, run once by `ship --act` when a PR is
deployed (e.g. reinstall a tool that tracks main). Never from a repo's .fleet.toml."""
import os, shutil, sys, tempfile, unittest
from datetime import datetime
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bin"))
sys.path.insert(0, os.path.join(HERE, "golden"))
from facts import load_ship  # noqa: E402
from test_golden import FIXTURES, Lenient, load, tighten_policy  # noqa: E402

S = load_ship()
DEPLOYED = load(next(p for p in FIXTURES if "deployed" in p))


def decide(after_deploy="", prev=None):
    fx = tighten_policy(DEPLOYED, after_deploy=after_deploy)
    fx["calls"]["prev"] = prev or {}
    s, pr, pol = S.decide(Lenient(fx), datetime.fromisoformat(fx["now"]))
    return s, pr, pol


class Decide(unittest.TestCase):
    def test_deployed_asks_to_run_it_once(self):
        s, _, _ = decide("make install")
        self.assertEqual((s["state"], s["act"]), ("deployed", "after-deploy"))

    def test_after_it_ran_the_pr_is_done(self):
        ran = {"ok": False, "at": "t", "tail": "boom"}
        s, _, _ = decide("make install", prev={"after_deploy": ran})
        self.assertEqual((s["state"], s["act"]), ("deployed", None))
        self.assertTrue(s["next"].startswith("done:"))

    def test_off_by_default(self):
        s, _, _ = decide()
        self.assertEqual((s["state"], s["act"]), ("deployed", None))


class Act(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()  # the command runs in ~, and the once-lock lives in the state dir
        self.addCleanup(shutil.rmtree, self.home, True)
        for p in (mock.patch.dict(os.environ, HOME=self.home), mock.patch.object(S, "STATE_DIR", self.home),
                  mock.patch.object(S, "ensure_label")):
            p.start()
            self.addCleanup(p.stop)

    def run_act(self, command):
        s, pr, pol = decide(command)
        return S.act(s, pr, pol), s

    def test_runs_in_home_and_records_success(self):
        done, s = self.run_act("pwd > where")
        self.assertEqual(done, "ran after_deploy")
        self.assertEqual(s["after_deploy"]["ok"], True)
        with open(os.path.join(self.home, "where")) as f:
            self.assertEqual(os.path.realpath(f.read().strip()), os.path.realpath(self.home))

    def test_a_second_process_doesnt_run_it_again(self):
        self.run_act("echo x >> count")
        done, _ = self.run_act("echo x >> count")  # a ship run that read the ledger before the first wrote
        self.assertEqual(done, "after_deploy already ran for this PR")
        with open(os.path.join(self.home, "count")) as f:
            self.assertEqual(f.read(), "x\n")

    def test_the_outcome_survives_later_records(self):
        _, s = self.run_act("true")
        S.record(dict(s, state="deployed"))
        later, _, _ = decide("true", prev=S.last_record(s["repo"], s["number"]))
        S.record(later)  # a later run's state record keeps it, so decide() never asks again
        self.assertEqual(S.last_record(s["repo"], s["number"])["after_deploy"]["ok"], True)

    def test_failure_is_recorded_not_retried(self):
        done, s = self.run_act("echo nope >&2; exit 3")
        self.assertEqual(done, "after_deploy failed (exit 3)")
        self.assertEqual((s["after_deploy"]["ok"], s["after_deploy"]["tail"]), (False, "nope"))
        again, _, _ = decide("echo nope >&2; exit 3", prev={"after_deploy": s["after_deploy"]})
        self.assertIsNone(again["act"])


if __name__ == "__main__":
    unittest.main()
