"""fleet server — manage the fleet's user services (the watcher; `fleet remote` uses the same code).

  fleet server status [--json]
  fleet server start|stop|restart
  fleet server install [--dry-run]
  fleet server uninstall
  fleet server logs [-n N]

Install and uninstall need a terminal. Without a unit, run `fleet watch` in a pane.
"""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

import fleet_proc
import fleet_serve

# Every user service fleet installs. update restarts the active ones; each knows its own commands.
SERVICES = {
    "watch": {"label": "fleet-watch", "args": ("watch",), "description": "fleet watch", "log": "watch.log",
              "cli": "fleet server", "pane": "fleet watch"},
    "remote": {"label": "fleet-remote", "args": ("remote", "serve"), "description": "fleet remote (read-only tailnet API)",
               "log": "remote.log", "cli": "fleet remote", "pane": "fleet remote serve",
               "sandbox": True},
}
# For the network-facing service: no privilege gain, only the socket families it uses, the syscalls
# an ordinary service needs. Filesystem sandboxing (ProtectSystem/ProtectHome) is left out: user
# units without user namespaces silently ignore it, and a setting that does nothing would mislead.
SANDBOX = ("NoNewPrivileges=yes", "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", "LockPersonality=yes",
           "RestrictRealtime=yes", "SystemCallArchitectures=native", "SystemCallFilter=@system-service",
           "SystemCallFilter=~@privileged @resources", "UMask=0077")
COMMANDS = ("install", "uninstall", "start", "stop", "restart", "logs")


class UsageError(Exception):
    """The caller needs a terminal or different arguments."""


def run(args, check=True):
    p = fleet_proc.run(args, timeout=30)
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip() or f"{' '.join(args)} exited {p.returncode}")
    return p


class Service:
    """One of SERVICES, run as a user unit (systemd) or agent (launchd)."""
    def __init__(self, name="watch"):
        spec = SERVICES[name]
        label = self.label = spec["label"]
        self.args, self.description, self.cli, self.pane = spec["args"], spec["description"], spec["cli"], spec["pane"]
        self.sandbox = SANDBOX if spec.get("sandbox") else ()
        self.kind = "launchd" if sys.platform == "darwin" else "systemd" if sys.platform.startswith("linux") else "none"
        self.log = Path.home() / ".local/state/fleet" / spec["log"]
        self.path = (Path.home() / "Library/LaunchAgents" / f"{label}.plist" if self.kind == "launchd"
                     else Path.home() / ".config/systemd/user" / f"{label}.service")
        self.domain = f"gui/{os.getuid()}"
        self.target = f"{self.domain}/{label}"

    def systemctl(self, *args, check=True):
        return run(["systemctl", "--user", *args], check=check)

    def launchctl(self, *args, check=True):
        return run(["launchctl", *args], check=check)

    def status(self):
        installed = enabled = active = False
        if self.kind == "systemd":
            p = self.systemctl("show", f"{self.label}.service", "--property=LoadState,UnitFileState,ActiveState", check=False)
            if p.returncode:
                raise RuntimeError(p.stderr.strip() or "cannot query the systemd user manager")
            props = dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)
            installed = props.get("LoadState") not in (None, "not-found")
            enabled = props.get("UnitFileState") in ("enabled", "enabled-runtime")
            active = props.get("ActiveState") == "active"
        elif self.kind == "launchd":
            installed = self.path.is_file()
            disabled = self.launchctl("print-disabled", self.domain)
            enabled = installed and f'"{self.label}" => true' not in disabled.stdout
            p = self.launchctl("print", self.target, check=False)
            active = p.returncode == 0 and "state = running" in p.stdout
        return {"kind": self.kind, "installed": installed, "enabled": enabled, "active": active}

    def control(self, action):
        if not self.status()["installed"]:
            raise RuntimeError(f"no unit installed; run `{self.cli} install` or run `{self.pane}` in a pane")
        if self.kind == "systemd":
            self.systemctl(action, f"{self.label}.service")
        elif action == "stop":
            # Unloading prevents KeepAlive from immediately restarting the service.
            loaded = self.launchctl("print", self.target, check=False)
            if loaded.returncode == 0:
                self.launchctl("bootout", self.target)
        else:
            if self.launchctl("print", self.target, check=False).returncode:
                self.launchctl("bootstrap", self.domain, str(self.path))
            self.launchctl("kickstart", *(["-k"] if action == "restart" else []), self.target)

    def render(self, executable, path):
        if self.kind == "launchd":
            return plistlib.dumps({
                "Label": self.label, "ProgramArguments": [str(executable), *self.args],
                "EnvironmentVariables": {"PATH": path, "PYTHONUNBUFFERED": "1"},
                "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30,
                "StandardOutPath": str(self.log), "StandardErrorPath": str(self.log),
            }).decode()
        if self.kind != "systemd":
            raise RuntimeError("service installation is supported on Linux and macOS")
        return (f"[Unit]\nDescription={self.description}\nAfter=default.target\n\n[Service]\n"
                f"ExecStart={unit_quote(str(executable), command=True)} {' '.join(self.args)}\n"
                "Restart=on-failure\nRestartSec=30\n"
                f"Environment={unit_quote('PATH=' + path)}\nEnvironment=PYTHONUNBUFFERED=1\n"
                "StandardOutput=journal\nStandardError=journal\n"
                + "".join(f"{line}\n" for line in self.sandbox) + "\n"
                "[Install]\nWantedBy=default.target\n")

    def install(self, dry_run):
        require_terminal()
        shell = os.environ.get("SHELL")
        if not shell or not os.path.isabs(shell):
            raise RuntimeError("SHELL must name your login shell with an absolute path")
        path = run([shell, "-lc", 'printf %s "$PATH"']).stdout
        if not path or any(c in path for c in "\r\n\0"):
            raise RuntimeError("login shell returned an empty or multiline PATH; remove startup output and retry")
        executable = Path(__file__).resolve().with_name("fleet")
        content = self.render(executable, path)
        print(f"{self.path}:\n{content}", end="")
        if dry_run or input("Install, enable and start this service? [y/N] ").strip().lower() not in ("y", "yes"):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.write_text(content)
        if self.kind == "systemd":
            self.systemctl("daemon-reload")
            self.systemctl("enable", "--now", f"{self.label}.service")
        else:
            self.launchctl("enable", self.target)
            if self.launchctl("print", self.target, check=False).returncode == 0:
                self.launchctl("bootout", self.target)
            self.launchctl("bootstrap", self.domain, str(self.path))
            self.launchctl("kickstart", self.target)

    def uninstall(self):
        require_terminal()
        if not self.path.is_file():
            raise RuntimeError(f"no unit at {self.path}; nothing to uninstall")
        print(f"Remove {self.path}:\n{self.path.read_text()}")
        if input("Stop, disable and remove this service? [y/N] ").strip().lower() not in ("y", "yes"):
            return
        if self.kind == "systemd":
            self.systemctl("disable", "--now", f"{self.label}.service")
        elif self.kind == "launchd":
            if self.launchctl("print", self.target, check=False).returncode == 0:
                self.launchctl("bootout", self.target)
            self.launchctl("disable", self.target)
        else:
            raise RuntimeError("no service manager on this platform")
        self.path.unlink()
        if self.kind == "systemd":
            self.systemctl("daemon-reload")

    def logs(self, n):
        if self.kind == "systemd":
            args = ["journalctl", "--user", "-u", self.label, "--no-pager", "-n", str(n)]
        elif self.kind == "launchd":
            args = ["tail", "-n", str(n), str(self.log)]
        else:
            raise RuntimeError("no service manager on this platform")
        p = subprocess.run(args)
        if p.returncode:
            raise RuntimeError(f"{args[0]} exited {p.returncode}")


def unit_quote(value, command=False):
    # systemd expands specifiers everywhere, and dollar variables in ExecStart.
    value = value.replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'


def require_terminal():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise UsageError("needs a terminal: run it yourself")


def status(service):
    error = None
    try:
        ping = fleet_serve.call("ping") or {}
    except (fleet_serve.ServerError, TimeoutError) as e:
        ping, error = {}, str(e)
    try:
        unit = service.status()
    except (OSError, RuntimeError) as e:
        unit = {"kind": service.kind, "installed": service.path.is_file(), "enabled": False, "active": False,
                "error": str(e)}
    result = {"running": bool(ping), "pid": ping.get("pid"), "version": ping.get("version"),
              "started": ping.get("started_at"), "last_pass": ping.get("last_pass"),
              "passes": ping.get("passes", 0), "events_connected": bool(ping.get("events", False)),
              "unit": unit, "log": str(service.log)}
    if error:
        result["error"] = error
    return result


def manage(service, command, argv, prog):
    """install|uninstall|start|stop|restart|logs for any of SERVICES; the exit code."""
    parser = argparse.ArgumentParser(prog=f"{prog} {command}")
    if command == "install":
        parser.add_argument("--dry-run", action="store_true")
    if command == "logs":
        parser.add_argument("-n", type=int, default=100)
    args = parser.parse_args(argv)
    if command == "logs" and args.n < 0:
        parser.error("-n must be nonnegative")
    try:
        if command == "install":
            service.install(args.dry_run)
        elif command == "uninstall":
            service.uninstall()
        elif command == "logs":
            service.logs(args.n)
        else:
            service.control(command)
    except UsageError as e:
        print(f"{prog}: {e}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, EOFError) as e:
        print(f"{prog}: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv):
    if any(a in ("-h", "--help", "help") for a in argv):
        print(__doc__.strip())
        return 0
    service = Service("watch")
    if argv[:1] and argv[0] in COMMANDS:
        return manage(service, argv[0], argv[1:], "fleet server")
    parser = argparse.ArgumentParser(prog="fleet server")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status").add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = status(service)
    except (OSError, RuntimeError) as e:
        print(f"fleet server: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result))
    else:
        print(f"fleet server: {'running' if result['running'] else 'not running'} (pid {result['pid'] or '-'}); "
              f"{service.kind}: {'active' if result['unit']['active'] else 'inactive'}")
        print(f"log: {result['log']}")
        for error in (result.get("error"), result["unit"].get("error")):
            if error:
                print(error, file=sys.stderr)
    return 0 if result["running"] else 1
