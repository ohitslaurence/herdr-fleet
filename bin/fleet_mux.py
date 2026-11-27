"""fleet_mux — the one place fleet talks to the terminal multiplexer that hosts the agents.

Two backends behind the same verbs:
  herdr  the herdr TUI (`herdr` CLI): panes in tabs in workspaces; status from herdr's screen detection.
  core   herdr-core (`hc`), which the Herdr app is a client of: named terminals with labels; status is
         only what the program reports itself over OSC 7501 (Claude Code does; see docs/herdr-core.md).

Pick with [mux] backend in the fleet config, or FLEET_MUX=herdr|core (the env wins). Inside a
terminal the backend that runs it is a better guess than the config, so here() checks both.

Every verb takes a ref: a herdr pane id or agent name, or an hc address (t7, box/t7) or name.
Status words are normalised to herdr's: idle, working, blocked, done, unknown.

CLI (for bash callers such as orch), JSON out unless noted:
  fleet_mux.py backend | here | agents | get <ref> | status <ref> (bare word)
  fleet_mux.py read <ref> [--lines N] [--visible] (text) | send <ref> <text> | keys <ref> <key>...
  fleet_mux.py spawn --cwd D [--name N] [--label k=v]... [--group G] [--kind K] -- <argv>...
  fleet_mux.py label <ref> k=v... | close <ref>
"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet_proc  # noqa: E402

BACKENDS = ("herdr", "core")
STATUSES = ("idle", "working", "blocked", "done", "unknown")
LABEL = "fleet."  # prefix of every label fleet writes on a core terminal


class MuxDown(fleet_proc.HerdrDown):
    """The multiplexer couldn't be asked (not running, not installed, bad answer). Not "no agents".
    Subclasses HerdrDown so existing `except HerdrDown` callers keep working."""


def backend():
    b = os.environ.get("FLEET_MUX")
    if not b:
        try:
            import fleet_config
            b = fleet_config.get("mux", "backend", "herdr")
        except Exception:
            b = "herdr"
    if b not in BACKENDS:
        raise MuxDown(f"unknown mux backend {b!r} (want one of {', '.join(BACKENDS)})")
    return b


def here():
    """This process's own terminal ref, or None outside one."""
    if backend() == "core":
        return os.environ.get("HC_TERMINAL_ID") or None
    return os.environ.get("HERDR_PANE_ID") or None


def _call(prog, args, timeout=10):
    try:
        p = fleet_proc.run([prog, *args], timeout=timeout)
    except FileNotFoundError:
        raise MuxDown(f"{prog} isn't on PATH") from None
    if p.returncode:
        raise MuxDown((p.stderr.strip().splitlines() or [f"`{prog} {' '.join(args)}` exited {p.returncode}"])[0][:200])
    return p.stdout


def _json(prog, args, timeout=10):
    out = _call(prog, args, timeout)
    try:
        return json.loads(out)
    except ValueError:
        raise MuxDown(f"unexpected answer from `{prog} {args[0]}`: {out.strip()[:80]!r}") from None


# ---- herdr -----------------------------------------------------------------------------------

def _herdr_row(a):
    return {"ref": a.get("pane_id"), "name": a.get("name") or a.get("label"), "kind": a.get("agent"),
            "status": a.get("agent_status") if a.get("agent_status") in STATUSES else "unknown",
            "cwd": a.get("foreground_cwd") or a.get("cwd"), "group": a.get("workspace_id"),
            "title": a.get("terminal_title_stripped") or "", "session": (a.get("agent_session") or {}).get("value"),
            "tab": a.get("tab_id"), "labels": {}}


class Herdr:
    def agents(self):
        r = _json("herdr", ["agent", "list"]).get("result")
        rows = r["agents"] if isinstance(r, dict) else r
        if not isinstance(rows, list):
            raise MuxDown("unexpected answer from `herdr agent list`")
        return [_herdr_row(a) for a in rows]

    def get(self, ref):
        return _herdr_row(_json("herdr", ["agent", "get", ref])["result"]["agent"])

    def read(self, ref, lines=None, visible=False):
        args = ["agent", "read", ref, "--source", "visible" if visible else "recent-unwrapped"]
        if lines and not visible:
            args += ["--lines", str(lines)]
        return _call("herdr", args)

    def send(self, ref, text):
        _call("herdr", ["agent", "prompt", ref, text], timeout=30)

    def keys(self, ref, *keys):
        _call("herdr", ["agent", "send-keys", ref, *keys])

    def spawn(self, argv, cwd, name=None, labels=None, group=None, kind=None):
        """A new tab in workspace `group` (default: this pane's), running argv. With kind, herdr
        starts it as a recognised agent so its status is tracked."""
        ws = group or os.environ.get("HERDR_WORKSPACE_ID")
        if not ws:
            raise MuxDown("herdr spawn needs a workspace (run inside a herdr pane, or pass a group)")
        created = _json("herdr", ["tab", "create", "--workspace", ws, "--cwd", cwd, "--label", name or "agent",
                                  "--no-focus"])["result"]
        pane = created["root_pane"]["pane_id"]
        if kind:
            fleet_proc.run(["herdr", "agent", "start", name or pane, "--kind", kind, "--pane", pane,
                            "--timeout", "90000", "--", *argv[1:]], timeout=120)
        else:
            _call("herdr", ["pane", "run", pane, " ".join(_sh(a) for a in argv)])
        return pane

    def label(self, ref, **kv):
        return False  # herdr panes carry no free-form labels; callers keep their own state

    def close(self, ref):
        try:
            tab = self.get(ref)["tab"]
        except (MuxDown, KeyError, TypeError):
            return
        if tab:
            fleet_proc.run(["herdr", "tab", "close", tab], timeout=10)


def _sh(a):
    import shlex
    return shlex.quote(a)


# ---- herdr-core ------------------------------------------------------------------------------

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


def _core_row(t):
    labels = t.get("labels") or {}
    fg = t.get("foreground") or {}
    return {"ref": t.get("address"), "name": t.get("name"), "kind": labels.get(LABEL + "kind") or t.get("agent")
            or fg.get("name"), "status": core_status(t), "cwd": fg.get("cwd") or t.get("cwd"),
            "group": labels.get(LABEL + "group"), "title": t.get("title") or "",
            "session": labels.get(LABEL + "session"), "labels": labels,
            "msg": (t.get("status") or {}).get("msg")}


AGENT_PROGRAMS = ("claude", "codex", "opencode", "pi")


class Core:
    def agents(self):
        """Terminals running an agent: a known agent program, or one fleet labelled with a kind."""
        return [r for r in (_core_row(t) for t in self._list()) if _is_agent(r)]

    def _list(self):
        """Every terminal with its labels and status. `hc ls` is a summary without them; the first
        line `hc events` prints is a full snapshot, so one call instead of a `get` per terminal."""
        import select, subprocess
        try:
            with subprocess.Popen(["hc", "events", "--json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) as p:
                ready, _, _ = select.select([p.stdout], [], [], 10)  # a local socket: slower is down
                first = p.stdout.readline() if ready else ""
                p.kill()
                err = "" if first else p.stderr.read()
        except FileNotFoundError:
            raise MuxDown("hc isn't on PATH") from None
        try:
            rows = json.loads(first)["terminals"]
        except (ValueError, KeyError, TypeError):
            lines = err.strip().splitlines()
            raise MuxDown(lines[0][:200] if lines else f"unexpected answer from `hc events`: {first.strip()[:80]!r}") from None
        return [t for t in rows if isinstance(t, dict)]

    def get(self, ref):
        return _core_row(_json("hc", ["get", ref, "--json"]))

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

    def spawn(self, argv, cwd, name=None, labels=None, group=None, kind=None):
        labels = dict(labels or {})
        if group:
            labels[LABEL + "group"] = group
        if kind:
            labels[LABEL + "kind"] = kind
        args = ["run", "--bg", "--json", "--cwd", cwd]
        if name:
            args += ["--name", name]
        for k, v in labels.items():
            args += ["--label", f"{k}={v}"]
        return _json("hc", [*args, "--", *argv], timeout=30)["address"]

    def label(self, ref, **kv):
        _call("hc", ["label", ref, *(f"{k}={v}" for k, v in kv.items())])
        return True

    def close(self, ref):
        fleet_proc.run(["hc", "kill", ref], timeout=10)
        fleet_proc.run(["hc", "rm", ref], timeout=10)


def herdr_shape(t):
    """An hc terminal as a `herdr agent list` row, for the callers that read those fields (the
    cockpit, the watcher, doctor). pane_id is the hc address; the workspace is the fleet group,
    else the checkout the terminal runs in, so one worktree's terminals still group together."""
    r = _core_row(t)
    return {"pane_id": r["ref"], "agent": r["kind"], "agent_status": r["status"], "cwd": t.get("cwd"),
            "foreground_cwd": r["cwd"], "workspace_id": r["group"] or r["cwd"], "tab_id": r["ref"],
            "label": r["name"], "terminal_title": r["title"], "terminal_title_stripped": r["title"],
            "agent_session": {"value": r["session"]} if r["session"] else None, "labels": r["labels"],
            "status_msg": r.get("msg")}


def herdr_rows():
    """Agent rows in herdr's shape on either backend. Raises MuxDown when it can't answer."""
    if backend() == "core":
        c = Core()
        return [herdr_shape(t) for t in c._list() if _is_agent(_core_row(t))]
    return fleet_proc.herdr_agents_raw()


def agent_row(ref):
    """One agent in herdr's shape, or None if the multiplexer doesn't know it."""
    try:
        if backend() == "core":
            return herdr_shape(_json("hc", ["get", ref, "--json"]))
        r = _json("herdr", ["agent", "get", ref]).get("result", {})
        return r.get("agent", r)
    except MuxDown:
        return None


def _is_agent(r):
    return r["kind"] in AGENT_PROGRAMS or (LABEL + "kind") in r["labels"]


def mux():
    return Core() if backend() == "core" else Herdr()


def agents():
    return mux().agents()


def status(ref):
    try:
        return mux().get(ref)["status"]
    except MuxDown:
        return "unknown"


# ---- CLI -------------------------------------------------------------------------------------

def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    cmd, rest = argv[0], argv[1:]
    m = mux()
    try:
        if cmd == "backend":
            print(backend())
        elif cmd == "here":
            print(here() or "")
        elif cmd == "agents":
            print(json.dumps(m.agents()))
        elif cmd == "get":
            print(json.dumps(m.get(rest[0])))
        elif cmd == "status":
            print(status(rest[0]))
        elif cmd == "read":
            lines = int(rest[rest.index("--lines") + 1]) if "--lines" in rest else None
            sys.stdout.write(m.read(rest[0], lines=lines, visible="--visible" in rest))
        elif cmd == "send":
            m.send(rest[0], rest[1])
        elif cmd == "keys":
            m.keys(rest[0], *rest[1:])
        elif cmd == "spawn":
            if "--" not in rest:
                raise SystemExit("fleet_mux spawn: put the command after --")
            opts, argv_ = rest[:rest.index("--")], rest[rest.index("--") + 1:]
            o = {"labels": {}}
            i = 0
            while i < len(opts):
                f, v = opts[i], opts[i + 1] if i + 1 < len(opts) else None
                if f == "--label":
                    k, _, val = v.partition("=")
                    o["labels"][k] = val
                elif f in ("--cwd", "--name", "--group", "--kind"):
                    o[f[2:]] = v
                else:
                    raise SystemExit(f"fleet_mux spawn: unknown flag {f}")
                i += 2
            print(m.spawn(argv_, o.pop("cwd", os.getcwd()), **o))
        elif cmd == "label":
            kv = dict(a.partition("=")[::2] for a in rest[1:])
            m.label(rest[0], **kv)
        elif cmd == "close":
            m.close(rest[0])
        else:
            raise SystemExit(f"fleet_mux: unknown command {cmd!r}; see --help")
    except MuxDown as e:
        print(f"fleet_mux: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
