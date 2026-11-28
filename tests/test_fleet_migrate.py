"""fleet migrate restarts a herdr agent on herdr-core with its session and its own flags, moves
only idle agents, leaves the herdr pane alone when the resume fails, and repoints fleet's records.
--keep leaves the herdr side as it was; a later run takes over the copy instead of starting another."""
import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
sys.path.insert(0, BIN)
import fleet_migrate as M  # noqa: E402
import fleet_mux  # noqa: E402


def row(pane, kind="claude", status="idle", sid="s-1", cwd="/nowhere", ws="w1"):
    return {"pane_id": pane, "agent": kind, "agent_status": status, "foreground_cwd": cwd, "workspace_id": ws,
            "agent_session": {"value": sid} if sid else None}


class CarryFlags(unittest.TestCase):
    def test_keeps_flags_and_their_values(self):
        self.assertEqual(M.carry_flags("claude", ["claude", "--dangerously-skip-permissions", "--model", "opus",
                                                  "--effort=high"]),
                         ["--dangerously-skip-permissions", "--model", "opus", "--effort=high"])

    def test_drops_the_old_session_choice_the_setup_note_and_the_first_prompt(self):
        self.assertEqual(M.carry_flags("claude", ["claude", "--dangerously-skip-permissions", "--append-system-prompt",
                                                  "Setup: still installing", "fix the login test"]),
                         ["--dangerously-skip-permissions"])
        self.assertEqual(M.carry_flags("claude", ["claude", "--continue"]), [])
        self.assertEqual(M.carry_flags("claude", ["claude", "--resume", "abc", "--verbose"]), ["--verbose"])

    def test_codex_behind_a_node_wrapper_and_an_earlier_resume(self):
        argv = ["node", "/x/bin/codex", "resume", "--dangerously-bypass-approvals-and-sandbox", "-m", "gpt-5", "old-id"]
        self.assertEqual(M.carry_flags("codex", argv), ["--dangerously-bypass-approvals-and-sandbox", "-m", "gpt-5"])
        self.assertEqual(M.carry_flags("codex", ["node", "/x/codex", "-c", "a=b", "do it"]), ["-c", "a=b"])


class Plan(unittest.TestCase):
    def plan(self, rows, picked=None, here=None, workers=None, reports=lambda k: k == "claude", argv=None):
        return M.plan(rows, lambda p: argv or ["claude", "--dangerously-skip-permissions"], {"p2": "fix"}, {"w1": "fix"},
                      workers or {}, here, picked, reports)

    def test_an_idle_agent_resumes_with_its_flags(self):
        (e,) = self.plan([row("p1")])
        self.assertEqual(e["argv"], ["claude", "--dangerously-skip-permissions", "--resume", "s-1"])
        self.assertEqual(e["group"], "nowhere")

    def test_who_stays_and_why(self):
        rows = [row("me"), row("busy", status="working"), row("oc", kind="opencode"), row("nosid", sid=None),
                row("cx", kind="codex")]
        why = {e["pane"]: e.get("skip", "") for e in self.plan(rows, here="me")}
        self.assertIn("another", why["me"])
        self.assertIn("idle", why["busy"])
        self.assertIn("can't resume", why["oc"])
        self.assertIn("session id", why["nosid"])
        self.assertIn("name it", why["cx"])

    def test_naming_one_moves_it_even_without_status_reports(self):
        (e,) = self.plan([row("cx", kind="codex"), row("p1")], picked={"cx"}, argv=["codex", "--yolo"])
        self.assertEqual(e["argv"][:2], ["codex", "resume"])
        (e,) = self.plan([row("p2")], picked={"fix"})  # by pane label
        self.assertNotIn("skip", e)
        (e,) = self.plan([dict(row("p3"), name="smoke")], picked={"smoke"})  # by herdr's agent name
        self.assertEqual(e["name"], "smoke")

    def test_the_terminal_is_labelled_with_its_session(self):
        (e,) = self.plan([row("p1")])
        self.assertEqual(e["labels"][fleet_mux.SESSION], "s-1")

    def test_a_session_with_a_live_copy_on_core_moves_to_it(self):
        live = M.copies([{"address": "t3", "labels": {fleet_mux.SESSION: "s-1"}},
                         {"address": "t4", "labels": {fleet_mux.SESSION: "s-2"}, "state": "finished"}])
        self.assertEqual(live, {"s-1": "t3"})
        a, b = M.plan([row("p1"), row("p2", sid="s-2")], lambda p: ["claude"], {}, {}, {}, None, copies=live)
        self.assertEqual(a["copy"], "t3")
        self.assertNotIn("copy", b)

    def test_an_orch_worker_keeps_its_worker_name(self):
        (e,) = self.plan([row("p1")], workers={"p1": ("/o/w1", {"name": "w1"})})
        self.assertEqual(e["name"], "w1")

    def test_a_checkout_gets_dev_context_labels(self):
        with tempfile.TemporaryDirectory() as d:
            os.system(f"git -C {d} init -q")
            (e,) = self.plan([row("p1", cwd=d)])
            self.assertEqual(e["labels"][fleet_mux.WORKTREE], os.path.realpath(d))
            self.assertEqual(e["labels"][fleet_mux.TASK], "fix")


class Repoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "fleet"
        (self.state / "ledger").mkdir(parents=True)
        self.orch = Path(self.tmp.name) / "orch"
        for name, pane, by in (("w1", "wA:p1", "wZ:p1"), ("w2", "wB:p1", "wA:p1")):
            (self.orch / name).mkdir(parents=True)
            (self.orch / name / "meta").write_text(f"name={name}\nkind=claude\npane={pane}\nby={by}\nmux=herdr\n")
        patcher = mock.patch.dict(os.environ, {"ORCH_HOME": str(self.orch)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_records_follow_the_agent(self):
        (self.state / "notes.json").write_text(json.dumps({"/w": {"note": "x", "pane": "wA:p1"}}))
        (self.state / "watch.json").write_text(json.dumps({"pending": {"wA:p1": {"pane": "wA:p1"}}, "inbox": [{"pane": "wA:p11"}]}))
        (self.state / "ledger" / "o__r__1.json").write_text(json.dumps({"pane": "wA:p1", "state": "deploying"}))
        M.repoint("wA:p1", "t7", state_dir=str(self.state), worker_dir=self.orch / "w1")
        self.assertEqual(json.loads((self.state / "notes.json").read_text())["/w"]["pane"], "t7")
        watch = json.loads((self.state / "watch.json").read_text())
        self.assertEqual(watch["pending"], {"t7": {"pane": "t7"}})
        self.assertEqual(watch["inbox"], [{"pane": "wA:p11"}])  # exact ids only
        self.assertEqual(json.loads((self.state / "ledger" / "o__r__1.json").read_text())["pane"], "t7")
        w1 = (self.orch / "w1" / "meta").read_text()
        self.assertIn("pane=t7\n", w1)
        self.assertIn("mux=core\n", w1)
        w2 = (self.orch / "w2" / "meta").read_text()  # its orchestrator moved: only by= changes
        self.assertIn("by=t7\n", w2)
        self.assertIn("mux=herdr\n", w2)


class Move(unittest.TestCase):
    def entry(self, name="fix"):
        return {"pane": "p1", "kind": "claude", "argv": ["claude", "--resume", "s"], "cwd": "/w", "name": name,
                "labels": {}, "group": "w"}

    def setUp(self):
        self.herdr, self.core = mock.Mock(), mock.Mock()
        self.core.spawn.return_value = "t7"
        self.core.get.return_value = {"state": "live"}
        patcher = mock.patch.object(M, "repoint")
        self.repoint = patcher.start()
        self.addCleanup(patcher.stop)
        p2 = mock.patch.object(M.time, "sleep")
        p2.start()
        self.addCleanup(p2.stop)

    def test_success_repoints_then_closes_the_herdr_pane(self):
        with mock.patch.object(M, "up", return_value=True):
            self.assertEqual(M.move(self.entry(), self.herdr, self.core, {}), "moved to t7")
        self.repoint.assert_called_once_with("p1", "t7", worker_dir=None)
        self.herdr.close.assert_called_once_with("p1")

    def test_a_resume_that_exits_leaves_the_herdr_pane(self):
        self.core.get.return_value = {"state": "finished"}
        self.core.read.return_value = "No conversation found with session ID: s\n"
        result = M.move(self.entry(), self.herdr, self.core, {})
        self.assertTrue(result.startswith("stays"), result)
        self.assertIn("No conversation found", result)
        self.core.close.assert_called_once_with("t7")
        self.herdr.close.assert_not_called()
        self.repoint.assert_not_called()

    def test_keep_leaves_the_herdr_pane_and_the_records(self):
        with mock.patch.object(M, "up", return_value=True):
            result = M.move(self.entry(), self.herdr, self.core, {}, keep=True)
        self.assertEqual(result, "copied to t7; the herdr pane stays")
        self.repoint.assert_not_called()
        self.herdr.close.assert_not_called()

    def test_a_later_run_takes_over_the_copy(self):
        e = dict(self.entry(), copy="t3")
        self.assertEqual(M.move(e, self.herdr, self.core, {}), "moved to its copy t3")
        self.core.spawn.assert_not_called()
        self.repoint.assert_called_once_with("p1", "t3", worker_dir=None)
        self.herdr.close.assert_called_once_with("p1")
        self.herdr.close.reset_mock()
        self.assertEqual(M.move(e, self.herdr, self.core, {}, keep=True), "already on core as t3")
        self.herdr.close.assert_not_called()

    def test_a_taken_name_falls_back_to_none_except_for_an_orch_worker(self):
        taken = fleet_mux.MuxDown('a live terminal is already named "fix"', kind="name_taken")
        self.core.spawn.side_effect = [taken, "t8"]
        with mock.patch.object(M, "up", return_value=True):
            self.assertEqual(M.move(self.entry(), self.herdr, self.core, {}), "moved to t8")
        self.assertIsNone(self.core.spawn.call_args.kwargs["name"])
        self.core.spawn.side_effect = taken
        result = M.move(self.entry(), self.herdr, self.core, {"p1": ("/o/fix", {"name": "fix"})})
        self.assertTrue(result.startswith("stays"), result)


if __name__ == "__main__":
    unittest.main()
