"""fleet_mux — the one place fleet talks to the terminal multiplexer that hosts the agents.

Two backends behind the same verbs:
  herdr  the herdr TUI (`herdr` CLI): panes in tabs in workspaces; status from herdr's screen detection.
  core   herdr-core (`hc`), which the Herdr app is a client of: named terminals with labels; status is
         only what the program reports itself over OSC 7501 (Claude Code does; see docs/herdr-core.md).

Pick with [mux] backend in the fleet config, or FLEET_MUX=herdr|core (the env wins); code that
knows which backend a thing lives on passes it: mux("core").

Every verb takes a ref: a herdr pane id or agent name, or an hc address (t7, box/t7) or name.
Agent rows are herdr's `agent list` rows on both backends (pane_id, agent, agent_status, cwd,
foreground_cwd, workspace_id, label, ...); on core pane_id is the hc address and workspace_id the
fleet group. Status words are herdr's: idle, working, blocked, done, unknown.

CLI (for bash callers such as orch), JSON out unless noted; help anywhere prints this:
  fleet mux backend | here | agents | get <ref> | status <ref> (bare word)
  fleet mux read <ref> [--lines N] [--visible] (text) | send <ref> <text> | keys <ref> <key>...
  fleet mux spawn --cwd D [--name N] [--label k=v]... [--group G] [--kind K] -- <argv>...   (core)
  fleet mux label <ref> k=v... (core) | close <ref>
"""
import json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet_proc  # noqa: E402

BACKENDS = ("herdr", "core")
LEGACY_BACKEND = "herdr"  # where anything recorded before backends existed (an orch meta without mux=) runs
STATUSES = ("idle", "working", "blocked", "done", "unknown")

# Labels fleet writes on a core terminal (docs/herdr-core.md). Read and write them by these names.
GROUP = "fleet.group"            # the worktree (or other unit) it belongs to: its workspace_id
KIND = "fleet.kind"              # the agent harness
BY = "fleet.by"                  # an orch worker's orchestrator terminal
WORKTREE = "fleet.worktree"      # a dev context's checkout
TASK = "fleet.task"              # a dev context's task name
REPO_ROOT = "fleet.repo_root"    # a dev context's repo
SESSION = "fleet.session"        # the harness session it resumed (fleet migrate)

AGENT_PROGRAMS = ("claude", "codex", "opencode", "pi")

# Unset in every program fleet starts on core. hc gives a terminal the environment of its own
# server, and an hc server first started from inside an agent (any `hc` call starts one) carries
# that agent's variables: its herdr pane, which an agent there would report and act as, and its
# Claude Code session markers. Claude Code started under another's CLAUDE_CODE_CHILD_SESSION runs
# as its child and saves no transcript, so a resumed session would silently stop being saved.
# `fleet doctor` warns when the server has them (the fix: restart it from a clean shell).
CALLER_ENV = ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_SOCKET_PATH", "HERDR_BIN_PATH",
              "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
              "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
              "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SSE_PORT", "CLAUDE_EFFORT", "CLAUDE_PID",
              "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED")


class MuxDown(fleet_proc.HerdrDown):
    """The multiplexer couldn't be asked (not running, not installed, bad answer). Not "no agents".
    Subclasses HerdrDown so existing `except HerdrDown` callers keep working. kind: hc's error kind
    (e.g. "name_taken") when it gave one."""

    def __init__(self, message, kind=None):
        super().__init__(message)
        self.kind = kind


def backend(override=None):
    b = override or os.environ.get("FLEET_MUX")
    if not b:
        try:
            import fleet_config
            b = fleet_config.get("mux", "backend", "herdr")
        except Exception:
            b = "herdr"
    if b not in BACKENDS:
        raise MuxDown(f"unknown mux backend {b!r} (want one of {', '.join(BACKENDS)})")
    return b


def is_core(override=None):
    """For the few callers whose structure differs by backend (dev's contexts, fleet new's launch):
    herdr has workspaces and tabs, core has only labelled terminals."""
    return backend(override) == "core"


def here(env=None):
    """This process's own terminal ref, or None outside one. An hc terminal started from a herdr
    pane would inherit its HERDR_PANE_ID, so HC_TERMINAL_ID (the innermost) wins on any backend."""
    env = os.environ if env is None else env
    return env.get("HC_TERMINAL_ID") or env.get("HERDR_PANE_ID") or None


def context_labels(worktree, task, repo_root):
    """What every terminal of a dev context carries on core (dev and fleet new both write them)."""
    return {WORKTREE: worktree, TASK: task, REPO_ROOT: repo_root}


def _call(prog, args, timeout=10, env=None, ok=(0,)):
    try:
        p = fleet_proc.run([prog, *args], timeout=timeout, **({"env": env} if env is not None else {}))
    except FileNotFoundError:
        raise MuxDown(f"{prog} isn't on PATH") from None
    if p.returncode not in ok:
        first, kind = (p.stderr.strip().splitlines() or [f"`{prog} {' '.join(args)}` exited {p.returncode}"])[0], None
        try:  # hc --json reports errors as {"error": {"message", "hint", "kind"}}
            err = json.loads(first)["error"]
            first, kind = err["message"], err.get("kind")
        except (ValueError, KeyError, TypeError):
            pass
        raise MuxDown(first[:200], kind)
    return p.stdout


def _json(prog, args, timeout=10, env=None):
    out = _call(prog, args, timeout, env)
    try:
        return json.loads(out)
    except ValueError:
        raise MuxDown(f"unexpected answer from `{prog} {args[0]}`: {out.strip()[:80]!r}") from None


# ---- herdr -----------------------------------------------------------------------------------

class Herdr:
    name = "herdr"

    def agents(self):
        return fleet_proc.herdr_agents()

    def get(self, ref):
        r = _json("herdr", ["agent", "get", ref]).get("result", {})
        return r.get("agent", r)

    def read(self, ref, lines=None, visible=False):
        args = ["agent", "read", ref, "--source", "visible" if visible else "recent-unwrapped"]
        if lines and not visible:
            args += ["--lines", str(lines)]
        return _call("herdr", args)

    def send(self, ref, text):
        _call("herdr", ["agent", "prompt", ref, text], timeout=30)

    def keys(self, ref, *keys):
        _call("herdr", ["agent", "send-keys", ref, *keys])

    def focus(self, ref):
        _call("herdr", ["agent", "focus", ref])
        return True

    def rename(self, ref, name):
        _call("herdr", ["pane", "rename", ref, name])

    def rename_group(self, group, name):
        _call("herdr", ["workspace", "rename", group, name])

    def pane_labels(self):
        """{pane id: its label} for every herdr pane."""
        return {p["pane_id"]: p.get("label") for p in _json("herdr", ["pane", "list"]).get("result", {}).get("panes", [])}

    def workspace_labels(self):
        """{workspace id: its label}."""
        return {w["workspace_id"]: w.get("label")
                for w in _json("herdr", ["workspace", "list"]).get("result", {}).get("workspaces", [])}

    def argv(self, ref):
        """The command line of the program in the pane's foreground (`fleet migrate` restarts it)."""
        info = _json("herdr", ["pane", "process-info", "--pane", ref]).get("result", {}).get("process_info", {})
        procs = info.get("foreground_processes") or []
        return list(procs[0].get("argv") or []) if procs else []

    def close(self, ref):
        """Close the pane, ending its program."""
        _call("herdr", ["pane", "close", ref])

    def spawn(self, *a, **k):
        raise MuxDown("herdr: agents start in tabs (orch, dev, fleet new), not through the seam")

    label = spawn


# ---- herdr-core ------------------------------------------------------------------------------

# OSC 7501 status reports: herdr-core shows an agent's status only when the agent sends them
STATUS_SINCE = {"claude": "2.1.295", "codex": "0.162.2"}  # codex: merged after the 0.162.1 release


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def reports_status(kind, version):
    """Whether this harness version tells herdr-core its status (else its sessions show unknown)."""
    since = STATUS_SINCE.get(kind)
    return bool(since and version and _vtuple(version) >= _vtuple(since))


def harness_version(kind):
    """The installed version of a configured harness, or None if it isn't installed or won't say."""
    import fleet_config
    cmd = fleet_config.agent_kinds()[kind]["cmd"][0]
    try:
        p = fleet_proc.run([cmd, "--version"], timeout=10)
    except (OSError, fleet_proc.subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+\.\d+\.\d+)", (p.stdout or "") + (p.stderr or ""))
    return m.group(1) if p.returncode == 0 and m else None


_HELP = {}


def takes_flag(prog, flag):
    """Whether `prog --help` lists flag (cached per program). Codex grew --no-daemon after
    0.153; passing it to an older one makes it exit at once."""
    if prog not in _HELP:
        try:
            _HELP[prog] = fleet_proc.run([prog, "--help"], timeout=10).stdout
        except (OSError, fleet_proc.subprocess.SubprocessError):
            _HELP[prog] = ""
    return flag in _HELP[prog].split() or f"{flag}," in _HELP[prog] or f"{flag}=" in _HELP[prog]


def core_status(t):
    """herdr's word for an hc terminal. A program that never reported has no status (unknown);
    a finished terminal is done. blocked and error both need a human. idle/done follow the
    Herdr apps' shared seen rule: done while turn > herdr.seen."""
    if t.get("state") == "finished":
        return "done"
    word = ((t.get("status") or {}).get("state") or "").lower()
    if word in ("blocked", "error"):
        return "blocked"
    if word == "working":
        return "working"
    if word in ("idle", "done"):
        seen = (t.get("labels") or {}).get("herdr.seen")
        if seen not in (None, "") and str(seen).isdigit():
            return "done" if int(t.get("turn") or 0) > int(seen) else "idle"
        return word
    return "unknown"


def is_agent(t):
    """A live hc terminal running an agent: a known agent program, or one fleet labelled with a
    kind. A finished one stays listed until removed, but its agent is gone (herdr drops the pane)."""
    labels = t.get("labels") or {}
    prog = (t.get("foreground") or {}).get("name") or t.get("agent")
    return t.get("state") != "finished" and (KIND in labels or prog in AGENT_PROGRAMS or t.get("agent") in AGENT_PROGRAMS)


def herdr_shape(t):
    """An hc terminal as a `herdr agent list` row. The workspace is the fleet group, else the
    checkout it runs in, so one worktree's terminals still group together."""
    labels = t.get("labels") or {}
    fg = t.get("foreground") or {}
    cwd = fg.get("cwd") or t.get("cwd")
    return {"pane_id": t.get("address"), "agent": labels.get(KIND) or t.get("agent") or fg.get("name"),
            "agent_status": core_status(t), "cwd": t.get("cwd"), "foreground_cwd": cwd,
            "workspace_id": labels.get(GROUP) or cwd, "tab_id": t.get("address"), "label": t.get("name"),
            "terminal_title": t.get("title") or "", "terminal_title_stripped": t.get("title") or "",
            "agent_session": None, "labels": labels, "status_msg": (t.get("status") or {}).get("msg"),
            "pid": t.get("pid"), "state": t.get("state")}


class Core:
    name = "core"

    def terminals(self):
        """Every hc terminal as hc describes it, labels and status included. `hc ls` is a summary
        without them; the first line `hc events` prints is a full snapshot: one call, not a `get`
        per terminal."""
        try:
            first, err = fleet_proc.first_line(["hc", "events", "--json"], timeout=10)
        except FileNotFoundError:
            raise MuxDown("hc isn't on PATH") from None
        try:
            rows = json.loads(first)["terminals"]
        except (ValueError, KeyError, TypeError):
            lines = err.strip().splitlines()
            raise MuxDown(lines[0][:200] if lines else f"unexpected answer from `hc events`: {first.strip()[:80]!r}") from None
        return [t for t in rows if isinstance(t, dict)]

    def agents(self):
        return [herdr_shape(t) for t in self.terminals() if is_agent(t)]

    def get(self, ref):
        return herdr_shape(_json("hc", ["get", ref, "--json"]))

    def read(self, ref, lines=None, visible=False):
        """visible: the screen only; else the screen plus `lines` of history above it."""
        args = ["read", ref]
        if lines and not visible:
            args += ["--lines", str(lines)]
        return _call("hc", args)

    def send(self, ref, text):
        _call("hc", ["send", ref, text], timeout=30)

    def keys(self, ref, *keys):
        _call("hc", ["keys", ref, *keys])

    def focus(self, ref):
        return False  # core has no window: the Herdr app (or `hc attach`) shows a terminal

    def rename(self, ref, name):
        self.label(ref, **{TASK: name})

    def rename_group(self, group, name):
        pass  # a group is its terminals' labels; rename() already relabelled the one that matters

    def spawn(self, argv, cwd, name=None, labels=None, group=None, kind=None):
        argv = list(argv)
        if kind == "codex" and "--no-daemon" not in argv and takes_flag(argv[0], "--no-daemon"):
            argv.insert(1, "--no-daemon")  # else Codex's daemon runs its commands outside the terminal
        labels = dict(labels or {})
        if group:
            labels[GROUP] = group
        if kind:
            labels[KIND] = kind
        args = ["run", "--bg", "--json", "--cwd", cwd]
        if name:
            args += ["--name", name]
        for k, v in labels.items():
            args += ["--label", f"{k}={v}"]
        clean = ["env", *(a for k in CALLER_ENV for a in ("-u", k))]
        return _json("hc", [*args, "--", *clean, *argv], timeout=30)["address"]

    def label(self, ref, **kv):
        _call("hc", ["label", ref, *(f"{k}={v}" for k, v in kv.items())])

    def close(self, ref):
        """End the program and remove the terminal. Killing one already finished is fine (hc exit 11)."""
        _call("hc", ["kill", ref], ok=(0, 11))
        _call("hc", ["rm", ref])


def mux(name=None):
    """The backend `name` names, else the configured one."""
    return Core() if is_core(name) else Herdr()


def agents():
    """Agent rows (herdr's shape) on the configured backend; MuxDown when it can't answer."""
    return mux().agents()


def get(ref, name=None):
    """One agent row, or None if the multiplexer doesn't know it (or can't be asked)."""
    try:
        return mux(name).get(ref)
    except MuxDown:
        return None


def status(ref, name=None):
    return (get(ref, name) or {}).get("agent_status") or "unknown"


# ---- CLI -------------------------------------------------------------------------------------

USAGE = __doc__.strip()


def _need(rest, n, what):
    if len(rest) < n:
        raise SystemExit(f"fleet mux: {what}")
    return rest


def _spawn_args(rest):
    if "--" not in rest:
        raise SystemExit("fleet mux spawn: put the command after --")
    opts, argv = rest[:rest.index("--")], rest[rest.index("--") + 1:]
    if not argv:
        raise SystemExit("fleet mux spawn: no command after --")
    o = {"labels": {}}
    if len(opts) % 2:
        raise SystemExit(f"fleet mux spawn: {opts[-1]} needs a value")
    for f, v in zip(opts[::2], opts[1::2]):
        if f == "--label":
            k, eq, val = v.partition("=")
            if not eq or not k:
                raise SystemExit(f"fleet mux spawn: --label takes key=value, not {v!r}")
            o["labels"][k] = val
        elif f in ("--cwd", "--name", "--group", "--kind"):
            o[f[2:]] = v
        else:
            raise SystemExit(f"fleet mux spawn: unknown flag {f}")
    return argv, o


def main(argv):
    if not argv or any(a in ("-h", "--help", "help") for a in argv):
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    try:
        m = mux()
        if cmd == "backend":
            print(backend())
        elif cmd == "here":
            print(here() or "")
        elif cmd == "agents":
            print(json.dumps(m.agents()))
        elif cmd == "get":
            print(json.dumps(m.get(_need(rest, 1, "get <ref>")[0])))
        elif cmd == "status":
            print(status(_need(rest, 1, "status <ref>")[0]))
        elif cmd == "read":
            _need(rest, 1, "read <ref> [--lines N] [--visible]")
            lines = int(rest[rest.index("--lines") + 1]) if "--lines" in rest else None
            sys.stdout.write(m.read(rest[0], lines=lines, visible="--visible" in rest))
        elif cmd == "send":
            m.send(*_need(rest, 2, "send <ref> <text>")[:2])
        elif cmd == "keys":
            m.keys(*_need(rest, 2, "keys <ref> <key>..."))
        elif cmd == "spawn":
            argv_, o = _spawn_args(rest)
            print(m.spawn(argv_, o.pop("cwd", os.getcwd()), **o))
        elif cmd == "label":
            _need(rest, 2, "label <ref> k=v...")
            m.label(rest[0], **dict(a.partition("=")[::2] for a in rest[1:]))
        elif cmd == "close":
            m.close(_need(rest, 1, "close <ref>")[0])
        else:
            raise SystemExit(f"fleet mux: unknown command {cmd!r}; see `fleet mux help`")
    except MuxDown as e:
        print(f"fleet mux: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
