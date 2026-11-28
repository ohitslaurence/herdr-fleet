"""fleet migrate — move agents from herdr panes onto herdr-core, keeping their conversations.

A running program can't change multiplexer, but its session can: each agent is started again on
core in the same directory, resuming its session (`claude --resume <id>`, `codex resume <id>`),
with the flags it was started with. Only an agent that is idle or done moves; its herdr pane
closes only once the new terminal is up, so a failed resume leaves it where it was. Fleet's
records of it (orch worker meta, ship ledgers, notes, the watcher's inbox) move to the new
terminal.

  fleet migrate [--dry-run] [--yes] [--keep] --all     every agent that can move
  fleet migrate [--dry-run] [--yes] [--keep] <pane>...  these (pane ids or names from `herdr agent list`)

--keep starts the copy on core and leaves everything else as it was: the herdr pane stays open
and fleet's records still point at it, so you can try core first. Both resume the same session:
type into one of them at a time. Run fleet migrate again without --keep to finish: it takes
over the copy already on core (closes the herdr pane, moves the records) instead of starting
another.

--all leaves out harnesses that don't report their status to herdr-core (Codex before 0.162.2,
opencode): on core fleet couldn't tell them working from idle. Name one to move it anyway.
Stop the watcher first (`fleet server stop`), and set [mux] backend = "core" before starting it
again: it holds its inbox in memory and follows one backend. Keep it stopped while you try
--keep copies, or it would prompt both.
"""
import os, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet_config, fleet_mux, fleet_state  # noqa: E402

STATE_DIR = fleet_config.STATE_DIR

# How each harness resumes a session, and which of its flags take a value (so a flag's value is
# kept and a positional prompt, the first instruction it was started with, is not re-sent).
RESUME = {
    "claude": lambda prog, flags, sid: [prog, *flags, "--resume", sid],
    "codex": lambda prog, flags, sid: [prog, "resume", *flags, sid],
}
VALUE_FLAGS = {
    "claude": {"--model", "--effort", "--permission-mode", "--add-dir", "--settings", "--mcp-config", "--agent",
               "--agents", "--fallback-model", "--allowedTools", "--allowed-tools", "--disallowedTools",
               "--disallowed-tools", "--setting-sources", "--plugin-dir", "--betas"},
    "codex": {"-m", "--model", "-c", "--config", "-s", "--sandbox", "-a", "--ask-for-approval", "-p", "--profile",
              "-C", "--cd", "--add-dir", "-i", "--image", "--local-provider"},
}
# Flags that belong to the old start, not the session: which session to open, and the one-off
# setup note fleet new passes on the system prompt.
DROP_BOOL = {"claude": {"--continue", "-c", "--fork-session"}, "codex": {"--last"}}
DROP_VALUE = {"claude": {"--resume", "-r", "--session-id", "--append-system-prompt", "--system-prompt"}, "codex": set()}

MOVABLE = ("idle", "done")
MOVE_NEXT = "next: set [mux] backend = \"core\", then `fleet server start` (stop it first if it's running)"
KEEP_NEXT = "next: try them on core with the watcher stopped; happy? `fleet migrate` the same agents without --keep"


def carry_flags(kind, argv):
    """The flags of a running harness's command line worth keeping on resume."""
    names = {kind, os.path.basename(fleet_config.agent_kinds().get(kind, {}).get("cmd", [kind])[0])}
    start = next((i for i, a in enumerate(argv) if os.path.basename(a) in names), None)
    if start is None:
        return []
    rest, out, i = argv[start + 1:], [], 0
    if kind == "codex" and rest[:1] == ["resume"]:
        rest = rest[1:]  # the subcommand; the session id it named goes with the other positionals
    values, drop_bool, drop_value = VALUE_FLAGS.get(kind, set()), DROP_BOOL.get(kind, set()), DROP_VALUE.get(kind, set())
    while i < len(rest):
        a = rest[i]
        name = a.split("=", 1)[0]
        takes = "=" not in a and (name in values or name in drop_value)
        has_value = takes and i + 1 < len(rest) and not (name in ("--resume", "-r") and rest[i + 1].startswith("-"))
        span = rest[i:i + 1 + (1 if has_value else 0)]
        i += len(span)
        if not a.startswith("-") or name in drop_bool or name in drop_value:
            continue  # a positional (the first prompt), or a flag that only meant something at the first start
        out += span
    return out


def git(cwd, *args):
    try:
        p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def context(cwd, task):
    """(group, labels) for a terminal in cwd: dev's context labels when it's in a git checkout."""
    top = git(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return os.path.basename(cwd.rstrip("/")) or cwd, {}
    common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir") or top
    repo = os.path.dirname(common) if os.path.basename(common) == ".git" else common
    return os.path.basename(top), fleet_mux.context_labels(top, task or os.path.basename(top), repo)


def orch_workers():
    import fleet_orch
    return {meta.get("pane"): (d, meta) for d, meta, _ in fleet_orch.workers(fleet_orch.home()) if meta.get("pane")}


def plan(rows, argv_of, names, tasks, workers, here, picked=None, reports=lambda kind: True, on_core=None):
    """One entry per herdr agent: what it would be started as on core, or why it stays.
    on_core maps a session id to a live core terminal fleet migrate already resumed it in."""
    out, on_core = [], on_core or {}
    for a in rows:
        pane, kind = a["pane_id"], a.get("agent")
        name = a.get("name") or names.get(pane)  # herdr's agent name, else the pane's label
        named = picked is not None and (pane in picked or name in picked)
        if picked is not None and not named:
            continue
        e = {"pane": pane, "kind": kind, "status": a.get("agent_status"), "name": name,
             "cwd": a.get("foreground_cwd") or a.get("cwd")}
        sid = (a.get("agent_session") or {}).get("value")
        worker = workers.get(pane)
        if worker:
            e["name"] = worker[1].get("name") or e["name"]  # orch drives a worker by its name
        if pane == here:
            e["skip"] = "this is the pane running fleet migrate: run it from another"
        elif e["status"] not in MOVABLE:
            e["skip"] = f"{e['status']}: only an idle or done agent moves (try again when it's waiting)"
        elif kind not in RESUME:
            e["skip"] = f"fleet can't resume a {kind} session yet"
        elif not sid:
            e["skip"] = "herdr doesn't know its session id"
        elif not named and not reports(kind):
            e["skip"] = f"{kind} here doesn't report its status to herdr-core (name it to move it anyway)"
        if "skip" not in e and sid in on_core:
            e["copy"] = on_core[sid]
        elif "skip" not in e:
            prog = fleet_config.agent_kinds().get(kind, {}).get("cmd", [kind])[0]
            e["argv"] = RESUME[kind](prog, carry_flags(kind, argv_of(pane)), sid)
            e["group"], e["labels"] = context(e["cwd"], tasks.get(a.get("workspace_id")))
            e["labels"][fleet_mux.SESSION] = sid
        out.append(e)
    return out


def swap(obj, old, new):
    """obj with every string equal to old (values and keys) replaced by new."""
    if isinstance(obj, dict):
        return {(new if k == old else k): swap(v, old, new) for k, v in obj.items()}
    if isinstance(obj, list):
        return [swap(v, old, new) for v in obj]
    return new if obj == old else obj


def repoint(old, new, state_dir=STATE_DIR, worker_dir=None):
    """Move fleet's records of a pane to its new terminal."""
    paths = [os.path.join(state_dir, f) for f in ("notes.json", "watch.json")]
    ledger = os.path.join(state_dir, "ledger")
    if os.path.isdir(ledger):
        paths += [os.path.join(ledger, f) for f in sorted(os.listdir(ledger)) if f.endswith(".json")]
    for p in paths:
        if os.path.exists(p):
            fleet_state.update(p, lambda d: _replace(d, swap(d, old, new)))
    import fleet_orch
    for d, meta, _ in fleet_orch.workers(fleet_orch.home()):
        if old not in meta.values():
            continue
        meta = {k: (new if v == old else v) for k, v in meta.items()}
        if str(d) == str(worker_dir):
            meta.update(mux="core", placement="core", workspace="", tab="")
        fleet_orch.write_meta(d, meta)


def _replace(d, new):
    d.clear()
    d.update(new) if isinstance(d, dict) else d.extend(new)


def up(core, ref, wait=6.0):
    """Whether the new terminal is still running its program after `wait` seconds (a resume that
    can't find its session exits at once)."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(0.5)
        try:
            if core.get(ref).get("state") == "finished":
                return False
        except fleet_mux.MuxDown:
            return False
    return True


def core_copies(terminals):
    """{session id: address} of the live core terminals fleet migrate started (all of them carry
    the session label; a --keep copy is one whose herdr pane is still open)."""
    return {(t.get("labels") or {})[fleet_mux.SESSION]: t.get("address") for t in terminals
            if t.get("state") != "finished" and (t.get("labels") or {}).get(fleet_mux.SESSION)}


def finish(e, ref, herdr, workers, how="moved to"):
    """Hand the agent over to its core terminal: fleet's records, then the herdr pane."""
    worker = workers.get(e["pane"])
    repoint(e["pane"], ref, worker_dir=worker and worker[0])
    try:
        herdr.close(e["pane"])
    except fleet_mux.MuxDown as err:
        return f"{how} {ref}, but its herdr pane didn't close ({err}): close {e['pane']} yourself"
    return f"{how} {ref}"


def move(e, herdr, core, workers, keep=False):
    if "copy" in e:
        return finish(e, e["copy"], herdr, workers, "moved to its copy")
    spawn = lambda name: core.spawn(e["argv"], e["cwd"], name=name, labels=e["labels"], group=e["group"], kind=e["kind"])
    try:
        try:
            ref = spawn(e["name"] if e["pane"] in workers else fleet_mux.core_name(e["name"]))
        except fleet_mux.MuxDown as err:
            unusable = err.kind == "name_taken" or (err.kind == "invalid_params" and "bad name" in str(err))
            if not e["name"] or not unusable or e["pane"] in workers:
                raise  # orch drives a worker by name: it can't move without it
            ref = spawn(None)  # taken (unique among live terminals), or id-shaped; its title says what it is
    except fleet_mux.MuxDown as err:
        return f"stays: herdr-core couldn't start it: {err}"
    if not up(core, ref):
        tail = ""
        try:
            tail = core.read(ref, visible=True).strip().splitlines()[-1:]
            core.close(ref)
        except (fleet_mux.MuxDown, IndexError):
            pass
        return f"stays: the resume exited at once{': ' + tail[0] if tail else ''}"
    if keep:
        return f"copied to {ref}; the herdr pane stays"
    return finish(e, ref, herdr, workers)


def describe(e, keep=False):
    who = f"{e['pane']}" + (f" ({e['name']})" if e.get("name") else "")
    if "skip" in e:
        return f"  stays  {who:32} {e['kind']}: {e['skip']}"
    if "copy" in e and keep:
        return f"  stays  {who:32} {e['kind']}: already on core as {e['copy']}"
    if "copy" in e:
        return f"  move   {who:32} {e['kind']}: to {e['copy']}, the copy already on core"
    return f"  move   {who:32} {e['kind']} in {e['cwd']}\n         as: {' '.join(e['argv'])}"


def gather(picked):
    herdr, core = fleet_mux.Herdr(), fleet_mux.Core()
    rows = herdr.agents()
    live = core.terminals()  # and fail now, before planning, if herdr-core isn't up
    names, tasks = herdr.pane_labels(), herdr.workspace_labels()
    versions = {}

    def reports(kind):
        if kind not in versions:
            versions[kind] = fleet_mux.reports_status(kind, fleet_mux.harness_version(kind))
        return versions[kind]

    def argv_of(pane):
        try:
            return herdr.argv(pane)
        except fleet_mux.MuxDown:
            return []

    workers = orch_workers()
    return herdr, core, workers, plan(rows, argv_of, names, tasks, workers, fleet_mux.here(), picked, reports,
                                       core_copies(live))


def main(argv):
    if not argv or any(a in ("-h", "--help", "help") for a in argv):
        print(__doc__.strip())
        return 0
    dry, yes, keep = "--dry-run" in argv, "--yes" in argv, "--keep" in argv
    rest = [a for a in argv if a not in ("--dry-run", "--yes", "--keep")]
    if rest == ["--all"]:
        picked = None
    elif rest and not any(a.startswith("-") for a in rest):
        picked = set(rest)
    else:
        print("fleet migrate: give --all or the agents to move (see fleet migrate --help)", file=sys.stderr)
        return 2
    try:
        herdr, core, workers, entries = gather(picked)
    except fleet_mux.MuxDown as err:
        print(f"fleet migrate: {err}", file=sys.stderr)
        return 3
    if picked:
        missing = picked - {e["pane"] for e in entries} - {e.get("name") for e in entries}
        if missing:
            print(f"fleet migrate: no herdr agent {', '.join(sorted(missing))}", file=sys.stderr)
            return 2
    verb, after = ("copy", KEEP_NEXT) if keep else ("move", MOVE_NEXT)
    for e in entries:
        print(describe(e, keep))
    todo = [e for e in entries if "skip" not in e and not (keep and "copy" in e)]
    if dry or not todo:
        print(f"{len(todo)} to {verb}" if dry else f"nothing to {verb}")
        return 0
    if not yes:
        if not sys.stdin.isatty():
            print("fleet migrate: pass --yes to move them without a terminal to ask on", file=sys.stderr)
            return 2
        if input(f"{verb} {len(todo)}? [y/N] ").strip().lower() != "y":
            return 1
    failed = 0
    for e in todo:
        try:
            now = herdr.get(e["pane"]).get("agent_status")
        except fleet_mux.MuxDown:
            now = "gone"
        if now not in MOVABLE:
            print(f"  {e['pane']}: stays: it's {now} now")
            failed += 1
            continue
        result = move(e, herdr, core, workers, keep)
        failed += result.startswith("stays")
        print(f"  {e['pane']}: {result}")
    print(after)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
