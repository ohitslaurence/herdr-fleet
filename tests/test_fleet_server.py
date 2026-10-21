"""Service tests never contact the host's user manager or watcher."""
import contextlib
import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import fleet_server as server


def reply(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class Server(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        home = patch("fleet_server.Path.home", return_value=Path(self.temp.name))
        home.start()
        self.addCleanup(home.stop)
        self.platform = patch.object(sys, "platform", "linux")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.runner = patch.object(server.subprocess, "run")
        self.run = self.runner.start()
        self.run.return_value = reply()
        self.addCleanup(self.runner.stop)
        self.ping_patch = patch.object(server.fleet_serve, "call", return_value=None)
        self.ping = self.ping_patch.start()
        self.addCleanup(self.ping_patch.stop)

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = server.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def terminal(self):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(sys.stdin, "isatty", return_value=True))
        stack.enter_context(patch.object(sys.stdout, "isatty", return_value=True))
        return stack

    def test_status_running_and_stopped_json(self):
        self.run.return_value = reply("LoadState=loaded\nUnitFileState=enabled\nActiveState=active\n")
        self.ping.return_value = {"pid": 42, "started_at": "then", "last_pass": "now", "passes": 812, "events": True}
        code, out, err = self.main("status", "--json")
        data = json.loads(out)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(data, {"running": True, "pid": 42, "version": None, "started": "then",
                               "last_pass": "now", "passes": 812, "events_connected": True,
                               "unit": {"kind": "systemd", "installed": True, "enabled": True, "active": True},
                               "log": str(Path(self.temp.name) / ".local/state/fleet/watch.log")})
        self.ping.return_value = None
        code, out, _ = self.main("status", "--json")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["running"])

    def test_status_timeout_and_missing_manager_still_one_object(self):
        self.ping.side_effect = TimeoutError("ping timed out")
        self.run.side_effect = FileNotFoundError("systemctl")
        code, out, err = self.main("status", "--json")
        self.assertEqual((code, err), (1, ""))
        self.assertIn("timed out", json.loads(out)["error"])
        self.assertIn("systemctl", json.loads(out)["unit"]["error"])

    def test_no_unit_refuses_start(self):
        self.run.return_value = reply("LoadState=not-found\nUnitFileState=\nActiveState=inactive\n")
        code, _, err = self.main("start")
        self.assertEqual(code, 1)
        self.assertIn("fleet watch", err)
        self.assertEqual(self.run.call_count, 1)

    def test_systemd_controls(self):
        self.run.return_value = reply("LoadState=loaded\n")
        for action in ("start", "stop", "restart"):
            self.assertEqual(self.main(action)[0], 0)
            self.assertEqual(self.run.call_args.args[0], ["systemctl", "--user", action, "fleet-watch.service"])
        self.run.return_value = reply(returncode=1, stderr="manager unavailable")
        self.assertEqual(self.main("restart")[0], 1)

    def test_install_requires_both_terminal_streams_even_dry_run(self):
        for stdin, stdout in ((False, True), (True, False), (False, False)):
            with patch.object(sys.stdin, "isatty", return_value=stdin), patch.object(sys.stdout, "isatty", return_value=stdout):
                with self.assertRaisesRegex(server.UsageError, "needs a terminal: run it yourself"):
                    server.require_terminal()
        for command in (("install",), ("install", "--dry-run"), ("uninstall",)):
            code, _, err = self.main(*command)
            self.assertEqual(code, 2)
            self.assertIn("needs a terminal: run it yourself", err)
        self.run.assert_not_called()

    def test_systemd_render_escapes_paths_and_uses_journal(self):
        unit = server.Service().render('/install a/percent%/cash$/fleet', '/path with "quotes":/bin%')
        self.assertIn('ExecStart="/install a/percent%%/cash$$/fleet" watch', unit)
        self.assertIn('Environment="PATH=/path with \\"quotes\\":/bin%%"', unit)
        self.assertIn("StandardOutput=journal", unit)

    def test_install_dry_run_and_confirmed_systemd(self):
        service = server.Service()
        self.run.return_value = reply("/login/bin:/usr/bin")
        with self.terminal(), patch.dict(server.os.environ, SHELL="/bin/zsh"), patch("builtins.input", return_value="yes") as confirm, contextlib.redirect_stdout(io.StringIO()):
            # Patch the terminal guard separately because output is captured for assertions.
            with patch.object(server, "require_terminal"):
                service.install(True)
                confirm.assert_not_called()
                self.assertFalse(service.path.exists())
                self.assertEqual(self.run.call_args.args[0], ["/bin/zsh", "-lc", 'printf %s "$PATH"'])
                service.install(False)
        unit = service.path.read_text()
        self.assertIn(str(Path(server.__file__).resolve().with_name("fleet")), unit)
        self.assertIn("/login/bin:/usr/bin", unit)
        self.assertEqual(self.run.call_args.args[0], ["systemctl", "--user", "enable", "--now", "fleet-watch.service"])
        with patch.object(server, "require_terminal"), patch("builtins.input", return_value="yes"), contextlib.redirect_stdout(io.StringIO()):
            service.uninstall()
        self.assertFalse(service.path.exists())
        self.assertEqual(self.run.call_args.args[0], ["systemctl", "--user", "daemon-reload"])

    def test_declined_install_and_bad_login_output_do_not_write(self):
        service = server.Service()
        with patch.object(server, "require_terminal"), patch.dict(server.os.environ, SHELL="/bin/sh"), patch("builtins.input", return_value="no"), contextlib.redirect_stdout(io.StringIO()):
            self.run.return_value = reply("/bin")
            service.install(False)
            self.assertFalse(service.path.exists())
            self.run.return_value = reply("welcome\n/bin")
            with self.assertRaisesRegex(RuntimeError, "multiline"):
                service.install(False)
        self.assertFalse(service.path.exists())

    def test_launchd_render_and_controls(self):
        with patch.object(sys, "platform", "darwin"):
            service = server.Service()
        plist = plistlib.loads(service.render('/install & a/fleet', '/bin:/custom path').encode())
        self.assertEqual(plist["ProgramArguments"], ['/install & a/fleet', 'watch'])
        self.assertEqual(plist["EnvironmentVariables"]["PATH"], '/bin:/custom path')
        self.assertEqual(plist["StandardErrorPath"], str(service.log))
        self.assertEqual(plist["Label"], "fleet-watch")
        service.path.parent.mkdir(parents=True)
        service.path.write_text(service.render('/install/fleet', '/bin'))
        self.run.return_value = reply("state = running")
        self.assertEqual(service.status(), {"kind": "launchd", "installed": True, "enabled": True, "active": True})
        service.control("restart")
        self.assertEqual(self.run.call_args.args[0], ["launchctl", "kickstart", "-k", service.target])
        service.control("stop")
        self.assertEqual(self.run.call_args.args[0], ["launchctl", "bootout", service.target])
        self.run.side_effect = lambda args, **kwargs: reply(returncode=1 if args[1:2] == ["print"] else 0)
        service.control("start")
        self.assertIn(["launchctl", "bootstrap", service.domain, str(service.path)], [c.args[0] for c in self.run.call_args_list])

    def test_launchd_install_uninstall(self):
        with patch.object(sys, "platform", "darwin"):
            service = server.Service()
        def fake(args, **kwargs):
            return reply("/bin" if args[0] == "/bin/sh" else "", 1 if args[1:2] == ["print"] else 0)
        self.run.side_effect = fake
        with patch.object(server, "require_terminal"), patch.dict(server.os.environ, SHELL="/bin/sh"), patch("builtins.input", return_value="yes"), contextlib.redirect_stdout(io.StringIO()):
            service.install(False)
            self.assertEqual(plistlib.loads(service.path.read_bytes())["Label"], "fleet-watch")
            service.uninstall()
        self.assertFalse(service.path.exists())
        self.assertIn(["launchctl", "enable", service.target], [c.args[0] for c in self.run.call_args_list])
        self.assertIn(["launchctl", "disable", service.target], [c.args[0] for c in self.run.call_args_list])

    def test_logs_and_usage(self):
        self.assertEqual(self.main("logs", "-n", "12")[0], 0)
        self.assertEqual(self.run.call_args.args[0], ["journalctl", "--user", "-u", "fleet-watch", "--no-pager", "-n", "12"])
        with patch.object(sys, "platform", "darwin"):
            self.assertEqual(self.main("logs", "-n", "0")[0], 0)
        self.assertEqual(self.run.call_args.args[0][:3], ["tail", "-n", "0"])
        for args in ((), ("status", "--bogus"), ("logs", "-n", "-1"), ("logs", "-n", "bad")):
            with self.assertRaises(SystemExit) as e:
                self.main(*args)
            self.assertEqual(e.exception.code, 2)


class Services(unittest.TestCase):
    def test_only_the_network_facing_service_is_sandboxed(self):
        remote = server.Service("remote")
        remote.kind = "systemd"
        unit = remote.render("/opt/fleet/bin/fleet", "/usr/bin")
        for line in server.SANDBOX:
            self.assertIn(f"\n{line}\n", unit)
        self.assertNotIn("ProtectHome", unit)  # ignored by user units without namespaces: not claimed
        watch = server.Service("watch")
        watch.kind = "systemd"
        self.assertNotIn("NoNewPrivileges", watch.render("/opt/fleet/bin/fleet", "/usr/bin"))

    def test_every_service_renders_its_own_command_for_both_managers(self):
        for name, spec in server.SERVICES.items():
            for platform in ("linux", "darwin"):
                with self.subTest(name=name, platform=platform), patch.object(sys, "platform", platform):
                    svc = server.Service(name)
                    unit = svc.render("/opt/fleet/bin/fleet", "/usr/bin:/bin")
                    if platform == "darwin":
                        self.assertEqual(plistlib.loads(unit.encode())["ProgramArguments"],
                                         ["/opt/fleet/bin/fleet", *spec["args"]])
                    else:
                        self.assertIn(f'ExecStart="/opt/fleet/bin/fleet" {" ".join(spec["args"])}', unit)
                    self.assertEqual(svc.log.name, spec["log"])

    def test_hints_name_only_their_own_commands(self):
        for name, spec in server.SERVICES.items():
            with self.subTest(name), patch.object(server.Service, "status", return_value={"installed": False}):
                with self.assertRaises(RuntimeError) as e:
                    server.Service(name).control("start")
                self.assertIn(f"`{spec['cli']} install`", str(e.exception))
                for other in server.SERVICES.values():
                    if other is not spec:
                        self.assertNotIn(other["cli"], str(e.exception))


if __name__ == "__main__":
    unittest.main()
