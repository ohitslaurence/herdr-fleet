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


def program(hc_run_argv):
    """The command an `hc run` call starts, without fleet's `env -u ...` wrapper."""
    cmd = hc_run_argv[hc_run_argv.index("--") + 1:]
    assert cmd[0] == "env", cmd
    i = 1
    while cmd[i] == "-u":
        i += 2
    return cmd[i:]


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
        env = dict(os.environ, FLEET_MUX=backend, PATH=f"{self.dir}:{SYSTEM_PATH}", HOME=self.dir)
        env.pop("HC_TERMINAL_ID", None)
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
        self.assertEqual([r["pane_id"] for r in rows], ["t2", "t3"])  # herdr's row shape on core too
        self.assertEqual(rows[1]["agent"], "codex")
        self.assertEqual(rows[0]["foreground_cwd"], "/w/sub")
        self.assertEqual(rows[0]["agent_status"], "working")
        self.assertEqual(len(self.calls()), 1)

    def fake_core(self, terminals=()):
        """A fake hc: `events` gives a snapshot of these terminals, `run` answers t9."""
        snap = {"seq": 1, "terminals": list(terminals)}
        self.fake("hc", f"print(json.dumps({snap!r} if sys.argv[1] == 'events' else {{'address': 't9'}}))")

    def run_argv(self):
        return next(c for c in self.calls() if c[0] == "run")

    def test_core_spawn_labels_group_and_kind(self):
        self.fake_core()
        p = self.run_mux("core", "spawn", "--cwd", "/w", "--name", "fix", "--group", "api-wt-fix", "--kind",
                         "claude", "--label", "fleet.by=t1", "--", "claude", "--effort", "high")
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "t9"), p.stderr)
        argv = self.run_argv()
        self.assertEqual(argv[:6], ["run", "--bg", "--json", "--cwd", "/w", "--name"])
        self.assertIn("fleet.group=api-wt-fix", argv)
        self.assertIn("fleet.kind=claude", argv)
        self.assertIn("fleet.by=t1", argv)
        self.assertEqual(program(argv), ["claude", "--effort", "high"])

    def test_core_spawn_opens_in_the_app_space_most_terminals_are_in(self):
        self.fake_core([term("t1", None, labels={"herdr.space": "sp1"}), term("t2", None, labels={"herdr.space": "sp2"}),
                        term("t3", None, labels={"herdr.space": "sp2"})])
        self.run_mux("core", "spawn", "--cwd", "/w", "--", "zsh")
        self.assertIn("herdr.space=sp2", self.run_argv())

    def test_core_spawn_uses_the_configured_space(self):
        os.makedirs(os.path.join(self.dir, ".config", "fleet"))
        with open(os.path.join(self.dir, ".config", "fleet", "config.toml"), "w") as f:
            f.write('[mux]\nspace = "spX"\n')
        self.fake_core([term("t1", None, labels={"herdr.space": "sp1"})])
        self.run_mux("core", "spawn", "--cwd", "/w", "--", "zsh")
        self.assertIn("herdr.space=spX", self.run_argv())

    def test_core_spawn_prefers_the_callers_space_and_keeps_one_given(self):
        self.fake_core([term("t1", None, labels={"herdr.space": "sp1"}), term("t2", None, labels={"herdr.space": "sp2"}),
                        term("t3", None, labels={"herdr.space": "sp2"})])
        terms = [term("t1", None, labels={"herdr.space": "sp1"}), term("t2", None, labels={"herdr.space": "sp2"})]
        self.assertEqual(fleet_mux.Core().space(terms, env={"HC_TERMINAL_ID": "t1"}), "sp1")
        self.assertIsNone(fleet_mux.Core().space([term("t1", None)], env={}))
        self.run_mux("core", "spawn", "--cwd", "/w", "--label", "herdr.space=mine", "--", "zsh")
        argv = self.run_argv()
        self.assertIn("herdr.space=mine", argv)
        self.assertNotIn("herdr.space=sp2", argv)

    def test_core_programs_start_without_the_hc_servers_agent_session(self):
        # hc gives a terminal its server's environment; a server started inside an agent has that
        # agent's markers. This fake hc runs the command the way hc would, from such an environment.
        keys = ["HERDR_PANE_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_USE_BEDROCK"]
        envlog, probe = os.path.join(self.dir, "env"), os.path.join(self.dir, "probe.py")
        with open(probe, "w") as f:
            f.write(f"import json, os\nopen({envlog!r}, 'w').write(json.dumps([k for k in {keys!r} if k in os.environ]))\n")
        self.fake("hc", f"import os, subprocess\n"
                        f"subprocess.run(sys.argv[sys.argv.index('--') + 1:], env=dict(os.environ, **dict.fromkeys({keys!r}, '1')))\n"
                        f"print(json.dumps({{'address': 't9'}}))")
        p = self.run_mux("core", "spawn", "--cwd", "/w", "--", sys.executable, probe)
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(envlog) as f:
            self.assertEqual(json.load(f), ["CLAUDE_CODE_USE_BEDROCK"])  # your own settings still pass

    def test_read_visible_is_screen_only(self):
        self.fake("hc", "print('screen')")
        self.run_mux("core", "read", "t2", "--visible")
        self.run_mux("core", "read", "t2", "--lines", "80")
        self.assertEqual(self.calls(), [["read", "t2"], ["read", "t2", "--lines", "80"]])

    def test_core_spawn_adds_no_daemon_for_a_codex_that_takes_it(self):
        self.fake("hc", "print(json.dumps({'address': 't9'}))")
        self.fake("codex", "print('      --no-daemon  run in this terminal')")
        self.run_mux("core", "spawn", "--cwd", "/w", "--kind", "codex", "--", "codex", "--yolo")
        argv = next(c for c in self.calls() if c[:1] == ["run"])
        self.assertEqual(program(argv), ["codex", "--no-daemon", "--yolo"])

    def test_core_spawn_leaves_an_older_codex_alone(self):
        # codex 0.153 has no --no-daemon: passing it makes codex exit at once
        self.fake("hc", "print(json.dumps({'address': 't9'}))")
        self.fake("codex", "print('      --no-alt-screen')")
        self.run_mux("core", "spawn", "--cwd", "/w", "--kind", "codex", "--", "codex", "--yolo")
        argv = next(c for c in self.calls() if c[:1] == ["run"])
        self.assertEqual(program(argv), ["codex", "--yolo"])

    def test_cli_help_anywhere_does_nothing_else(self):
        self.fake("hc", "print(json.dumps({'address': 't9'}))")
        p = self.run_mux("core", "spawn", "--cwd", "/w", "--", "claude", "--help")
        self.assertIn("fleet mux backend", p.stdout)
        self.assertFalse(os.path.exists(self.log))

    def test_cli_bad_spawn_flags_say_so(self):
        p = self.run_mux("core", "spawn", "--cwd", "/w", "--label", "--", "x")
        self.assertIn("needs a value", p.stderr)
        p = self.run_mux("core", "spawn", "--label", "novalue", "--", "x")
        self.assertIn("key=value", p.stderr)

    def test_core_close_tolerates_an_already_finished_program(self):
        self.fake("hc", "sys.exit(11 if sys.argv[1] == 'kill' else 0)")
        p = self.run_mux("core", "close", "t9")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.calls(), [["kill", "t9"], ["rm", "t9"]])

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

    def test_hc_error_kind_is_kept(self):
        err = {"error": {"kind": "name_taken", "message": 'a live terminal is already named "fix"', "hint": "x"}}
        self.fake("hc", f"sys.stderr.write(json.dumps({err!r}) + '\\n'); sys.exit(1)")
        with mock.patch.dict(os.environ, {"PATH": f"{self.dir}:{SYSTEM_PATH}"}):
            with self.assertRaises(fleet_mux.MuxDown) as cm:
                fleet_mux.Core().spawn(["claude"], "/w", name="fix")
        self.assertEqual((cm.exception.kind, str(cm.exception)), ("name_taken", 'a live terminal is already named "fix"'))

    def test_down_is_an_error_not_empty(self):
        self.fake("hc", "sys.stderr.write('core is not running\\n'); sys.exit(1)")
        p = self.run_mux("core", "agents")
        self.assertEqual(p.returncode, 3)
        self.assertIn("core is not running", p.stderr)
        self.assertEqual(p.stdout, "")



class CoreName(unittest.TestCase):
    def test_a_name_becomes_one_core_takes(self):
        self.assertEqual(fleet_mux.core_name("Fix: Login!"), "fix--login")
        self.assertEqual(len(fleet_mux.core_name("x" * 50)), fleet_mux.NAME_MAX)
        self.assertIsNone(fleet_mux.core_name("--"))
        self.assertIsNone(fleet_mux.core_name(None))

    def test_the_changelog_states_the_limit(self):
        with open(os.path.join(BIN, "..", "CHANGELOG.md")) as f:
            changelog = f.read()
        self.assertIn(f"({fleet_mux.NAME_MAX} characters)", changelog)


if __name__ == "__main__":
    unittest.main()
