"""fleet_mux speaks to herdr or herdr-core through the same verbs: status words mean the same on
both, a core terminal's status is only what its program reported, and a down multiplexer is an
error, never "no agents"."""
import json, os, stat, subprocess, sys, tempfile, unittest
from unittest import mock

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
sys.path.insert(0, BIN)
import fleet_mux  # noqa: E402
from system_path import SYSTEM_PATH  # noqa: E402


def term(addr, status=None, turn=0, labels=None, state="live", agent="claude", name=None):
    return {"address": addr, "name": name, "agent": agent, "state": state, "turn": turn, "cwd": "/w",
            "labels": labels or {}, "status": status, "foreground": {"name": agent, "cwd": "/w/sub"}}


class CoreStatus(unittest.TestCase):
    def test_words_map_to_herdrs(self):
        self.assertEqual(fleet_mux.core_status(term("t1", {"state": "working"})), "working")
        self.assertEqual(fleet_mux.core_status(term("t1", {"state": "blocked"})), "blocked")
        self.assertEqual(fleet_mux.core_status(term("t1", {"state": "error"})), "blocked")
        self.assertEqual(fleet_mux.core_status(term("t1", {"state": "idle"})), "idle")

    def test_never_reported_is_unknown_not_idle(self):
        self.assertEqual(fleet_mux.core_status(term("t1", None)), "unknown")

    def test_finished_terminal_is_done(self):
        self.assertEqual(fleet_mux.core_status(term("t1", None, state="finished")), "done")

    def test_seen_label_decides_done_or_idle(self):
        unseen = term("t1", {"state": "done"}, turn=3, labels={"herdr.seen": "2"})
        seen = term("t1", {"state": "done"}, turn=3, labels={"herdr.seen": "3"})
        self.assertEqual(fleet_mux.core_status(unseen), "done")
        self.assertEqual(fleet_mux.core_status(seen), "idle")


class Backend(unittest.TestCase):
    def test_env_wins_and_bad_names_fail(self):
        with mock.patch.dict(os.environ, {"FLEET_MUX": "core"}):
            self.assertEqual(fleet_mux.backend(), "core")
        with mock.patch.dict(os.environ, {"FLEET_MUX": "tmux"}):
            with self.assertRaises(fleet_mux.MuxDown):
                fleet_mux.backend()

    def test_here_is_the_innermost_terminal(self):
        # an hc terminal started from a herdr pane inherits HERDR_PANE_ID: it is still the hc terminal
        self.assertEqual(fleet_mux.here({"HC_TERMINAL_ID": "t4", "HERDR_PANE_ID": "w1:p2"}), "t4")
        self.assertEqual(fleet_mux.here({"HERDR_PANE_ID": "w1:p2"}), "w1:p2")
        self.assertIsNone(fleet_mux.here({}))


class FakeBins(unittest.TestCase):
    """Real subprocesses against fake hc/herdr scripts that log their argv."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.log = os.path.join(self.dir, "argv.log")

    def fake(self, name, body):
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write(f"#!/usr/bin/env python3\nimport json, sys\nopen({self.log!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n{body}\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def run_mux(self, backend, *args):
        env = dict(os.environ, FLEET_MUX=backend, PATH=f"{self.dir}:{SYSTEM_PATH}")
        return subprocess.run([sys.executable, os.path.join(BIN, "fleet_mux.py"), *args], env=env,
                              capture_output=True, text=True, timeout=30)

    def calls(self):
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def test_core_agents_come_from_one_snapshot_and_skip_shells(self):
        snap = {"seq": 1, "terminals": [term("t1", None, agent="zsh"), term("t2", {"state": "working"}),
                                         term("t3", None, agent="node", labels={"fleet.kind": "codex"})]}
        self.fake("hc", f"print(json.dumps({snap!r}), flush=True)\nimport time; time.sleep(30)")
        p = self.run_mux("core", "agents")
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = json.loads(p.stdout)
        self.assertEqual([r["ref"] for r in rows], ["t2", "t3"])
        self.assertEqual(rows[1]["kind"], "codex")
        self.assertEqual(rows[0]["cwd"], "/w/sub")
        self.assertEqual(len(self.calls()), 1)

    def test_core_spawn_labels_group_and_kind(self):
        self.fake("hc", "print(json.dumps({'address': 't9'}))")
        p = self.run_mux("core", "spawn", "--cwd", "/w", "--name", "fix", "--group", "api-wt-fix", "--kind",
                         "claude", "--label", "fleet.by=t1", "--", "claude", "--effort", "high")
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "t9"), p.stderr)
        argv = self.calls()[0]
        self.assertEqual(argv[:6], ["run", "--bg", "--json", "--cwd", "/w", "--name"])
        self.assertIn("fleet.group=api-wt-fix", argv)
        self.assertIn("fleet.kind=claude", argv)
        self.assertIn("fleet.by=t1", argv)
        self.assertEqual(argv[argv.index("--") + 1:], ["claude", "--effort", "high"])

    def test_core_spawn_drops_herdrs_variables(self):
        self.fake("hc", "import os; open(os.environ['ENVLOG'], 'w').write(os.environ.get('HERDR_PANE_ID', 'unset')); print(json.dumps({'address': 't9'}))")
        envlog = os.path.join(self.dir, "env")
        env = dict(os.environ, FLEET_MUX="core", PATH=f"{self.dir}:{SYSTEM_PATH}", HERDR_PANE_ID="w1:p1", ENVLOG=envlog)
        subprocess.run([sys.executable, os.path.join(BIN, "fleet_mux.py"), "spawn", "--cwd", "/w", "--", "claude"],
                       env=env, capture_output=True, timeout=30)
        with open(envlog) as f:
            self.assertEqual(f.read(), "unset")

    def test_read_visible_is_screen_only(self):
        self.fake("hc", "print('screen')")
        self.run_mux("core", "read", "t2", "--visible")
        self.run_mux("core", "read", "t2", "--lines", "80")
        self.assertEqual(self.calls(), [["read", "t2"], ["read", "t2", "--lines", "80"]])

    def test_herdr_verbs_keep_todays_commands(self):
        self.fake("herdr", "print(json.dumps({'result': {'agent': {'pane_id': 'w1:p2', 'agent_status': 'idle'}}}))")
        self.run_mux("herdr", "send", "fix", "do it")
        self.run_mux("herdr", "keys", "fix", "down", "enter")
        self.run_mux("herdr", "read", "fix", "--lines", "80")
        p = self.run_mux("herdr", "status", "fix")
        self.assertEqual(p.stdout.strip(), "idle")
        self.assertEqual(self.calls(), [["agent", "prompt", "fix", "do it"], ["agent", "send-keys", "fix", "down", "enter"],
                                        ["agent", "read", "fix", "--source", "recent-unwrapped", "--lines", "80"],
                                        ["agent", "get", "fix"]])

    def test_down_is_an_error_not_empty(self):
        self.fake("hc", "sys.stderr.write('core is not running\\n'); sys.exit(1)")
        p = self.run_mux("core", "agents")
        self.assertEqual(p.returncode, 3)
        self.assertIn("core is not running", p.stderr)
        self.assertEqual(p.stdout, "")


if __name__ == "__main__":
    unittest.main()
