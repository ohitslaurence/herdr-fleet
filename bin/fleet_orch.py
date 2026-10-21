#!/usr/bin/env python3
"""fleet_orch — the parts of `orch` that bash gets wrong: brief rendering, reading an agent's
screen, picking values out of herdr JSON, and input validation. Every value arrives via argv or
stdin; nothing here builds code from strings.

CLI (for bin/orch):
  fleet_orch.py render <template> <out> NAME=<n> STATUS=<p> REPORT=<p> WORKTREE=<p>
      fills {{NAME}} {{STATUS}} {{REPORT}} {{WORKTREE}}; refuses a template with any other {{...}} left
  fleet_orch.py classify <kind> <worktree> < screen
      ready | trust | hooks | waiting | human: <why>   (trust/hooks: orch may answer the dialog itself)
  fleet_orch.py delivered <kind> <brief-first-line> < screen
      working | typed | empty | unknown               (what the screen says after `agent prompt`)
  fleet_orch.py unfinished <worktree>
      one line naming what a "done" worker left behind (uncommitted changes, unpushed commits), or nothing
  fleet_orch.py status <state-dir> [--json]
      text or JSON status from the same worker rows
  fleet_orch.py pick <dotted.path> [<key>=<value> <dotted.field>] < json
      a value from herdr/dev JSON; with a match, the element of the list at <path> whose <key> is <value>
"""
import json, os, re, sys, time
from pathlib import Path

import fleet_proc

PLACEHOLDERS = ("NAME", "STATUS", "REPORT", "WORKTREE")

# Dialogs a worker can't answer for itself: a login, a mode acceptance, a yes/no the brief
# would otherwise be typed into.
HUMAN_RE = re.compile(r"Do you want|Yes, I accept|Sign in|Press Enter")
# The composer itself (not the ❯ cursor, which every Claude dialog also draws).
READY_RE = {
    "claude": re.compile(r"bypass permissions on|\? for shortcuts"),
    "codex": re.compile(r"Ask Codex to do anything"),
    "opencode": re.compile(r"ctrl\+p commands|tab agents|Ask anything"),
}
# An empty composer: the placeholder is back and nothing was typed.
EMPTY_RE = {
    "claude": re.compile(r"^[\s│]*[❯>]\s*(Try |│\s*$|$)", re.M),
    "codex": READY_RE["codex"],
    "opencode": re.compile(r"Ask anything"),
}
# not "working:": the brief's own status-protocol text says it, so an unsent brief would match
WORKING_RE = re.compile(r"Working|esc to interrupt|esc interrupt")
# Claude's trust dialog (wording varies by version) lists what the folder's settings would grant;
# any of these is a human's call.
TRUST_RE = re.compile(r"trust (this|the files in this) folder")
TRUST_EXTRA_RE = re.compile(r"pre-approves|runs commands|adds .* director|to the workspace", re.I)
PATH_RE = re.compile(r"(?<![\w])(?:~|\$HOME|/)[^\s'\"`]*")


def render(template, out, values):
    text = Path(template).read_text()
    for k in PLACEHOLDERS:
        text = text.replace("{{%s}}" % k, values[k])
    if left := sorted(set(re.findall(r"\{\{[^}]*\}\}", text))):
        raise SystemExit(f"brief still has placeholders: {' '.join(left)}")
    with open(out, "w") as f:
        f.write(text)


def classify(kind, worktree, screen, home=None):
    """What the agent's screen is showing while orch waits to deliver the brief."""
    home = home or os.path.expanduser("~")
    if kind == "claude" and TRUST_RE.search(screen):
        if TRUST_EXTRA_RE.search(screen):
            return "human: the trust prompt lists extra permissions"
        if os.path.basename(worktree) not in screen:
            return "human: the trust prompt is for another folder"
        return "trust"
    if kind == "codex" and "Hooks need review" in screen:
        return hooks_verdict(screen, worktree, home)
    if m := HUMAN_RE.search(screen):
        return f"human: dialog says '{m.group(0)}'"
    if READY_RE[kind].search(screen):
        return "ready"
    return "waiting"


def hooks_verdict(screen, worktree, home):
    """Codex's hook review: trust only when every hook it lists is the user's own, under ~/.codex.
    A repo can ship .codex/hooks.json; that one a human reads first."""
    paths = [p.replace("$HOME", home).replace("~", home, 1) if p.startswith(("~", "$HOME")) else p
             for p in PATH_RE.findall(screen)]
    if not paths:
        return "human: the hooks prompt names no files orch can check"
    own = os.path.join(home, ".codex") + "/"
    if (worktree and worktree in screen) or any(not p.startswith(own) for p in paths):
        return "human: the hooks prompt lists hooks outside ~/.codex"
    return "hooks"


def delivered(kind, first_line, screen):
    """After `agent prompt`: did the brief land? `typed` means its first line is on screen, sent or
    still in the composer; either way a second send would double it."""
    flat = " ".join(screen.split())
    if WORKING_RE.search(screen):
        return "working"
    if first_line and " ".join(first_line.split())[:40] in flat:
        return "typed"
    if EMPTY_RE[kind].search(screen):
        return "empty"
    return "unknown"


def unfinished(worktree):
    """What a worker that says it's done left behind in its worktree. A worker's "done" is a claim:
    a PR it never pushed or edits it never committed are lost when `orch done` removes the worktree.
    A scout that changed nothing has nothing to report."""
    if not os.path.isdir(worktree):
        return []

    def git(*args):
        return fleet_proc.run(["git", "-C", worktree, *args], timeout=20)
    dirty = git("status", "--porcelain")
    # commits no remote branch has: pushed work is on some origin/* ref, a scout's branch has none
    ahead = git("rev-list", "--count", "HEAD", "--not", "--remotes")
    if dirty.returncode or ahead.returncode or not ahead.stdout.strip().isdigit():
        return ["can't check the worktree (git failed)"]
    out = []
    lines = dirty.stdout.splitlines()
    untracked = sum(line.startswith("??") for line in lines)
    if changed := len(lines) - untracked:
        out.append(f"{changed} uncommitted change{'s' * (changed != 1)}")
    if untracked:
        out.append(f"{untracked} untracked file{'s' * (untracked != 1)}")
    if (n := int(ahead.stdout)) and not pushed_then_deleted(git):
        out.append(f"{n} commit{'s' * (n != 1)} not pushed")
    return out


def pushed_then_deleted(git):
    """The branch was pushed (`push -u` made origin/<branch> its upstream), then merged, deleted on
    GitHub and pruned here: its commits are on no remote ref, but nothing was lost. Only git
    saying exactly that counts: a failed call is no excuse."""
    branch = git("branch", "--show-current")
    if branch.returncode or not branch.stdout.strip():
        return False
    name = branch.stdout.strip()
    merge = git("config", f"branch.{name}.merge")
    upstream = git("rev-parse", "--verify", "-q", "@{upstream}")
    # rev-parse --verify -q: 1 and no output when the ref is missing; anything else is a failure
    return merge.returncode == 0 and merge.stdout.strip() == f"refs/heads/{name}" \
        and upstream.returncode == 1 and not upstream.stderr.strip()


def pick(data, path, match=None, field=None):
    cur = data
    for part in path.split("."):
        cur = cur[part]
    if match:
        key, _, value = match.partition("=")
        cur = next(e for e in cur if str(e.get(key)) == value)
        for part in field.split("."):
            cur = cur[part]
    return cur


def home():
    return os.environ.get("ORCH_HOME") or os.path.expanduser("~/.local/state/orch")


def _read(path):
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def workers(home):
    """(state dir, meta dict, last status line) for every worker under home."""
    for path in sorted(Path(home).glob("*/meta")):
        meta = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
        lines = _read(path.parent / "status").splitlines()
        yield path.parent, meta, lines[-1] if lines else ""


def status(home):
    rows = []
    for directory, meta, last in workers(home):
        try:
            p = fleet_proc.run(["herdr", "agent", "get", directory.name], timeout=10)
            agent = json.loads(p.stdout)["result"]["agent"]["agent_status"] if p.returncode == 0 else "-"
        except (OSError, ValueError, KeyError, TypeError):
            agent = "-"
        turn = directory / "turn-ended"
        report = directory / "report.md"
        rows.append({"name": directory.name, "kind": meta.get("kind", ""), "herdr": agent,
                        "state": last.partition(":")[0], "status": last,
                        "turn_minutes": int((time.time() - turn.stat().st_mtime) / 60) if turn.exists() else None,
                        "report": report.is_file() and report.stat().st_size > 0})
    return {"workers": rows}


TERMINAL = ("done", "failed", "blocked", "needs-decision")


def unreported(home):
    """Workers whose last status line is terminal, not yet shown by `orch wait` (the `reported`
    file) and not yet announced by fleet watch (`notified`), as {orchestrator pane: [(name, state,
    line)]}. Workers with no recorded orchestrator are left out."""
    out = {}
    for directory, meta, last in workers(home):
        state = last.partition(":")[0]
        shown = {_read(directory / "reported").strip(), _read(directory / "notified").strip()}
        if meta.get("by") and state in TERMINAL and last not in shown:
            out.setdefault(meta["by"], []).append((directory.name, state, last))
    return out


def mark_notified(home, name, line):
    (Path(home) / name / "notified").write_text(line + "\n")


def print_status(home, as_json=False):
    data = status(home)
    if as_json:
        print(json.dumps(data))
        return
    print(f"{'NAME':22} {'KIND':8} {'HERDR':9} {'STATE':16} {'TURN':6} REPORT")
    for row in data["workers"]:
        turn = f"{row['turn_minutes']}m" if row["turn_minutes"] is not None else "-"
        report = "yes" if row["report"] else "-"
        print(f"{row['name']:22} {row['kind']:8} {row['herdr']:9} {row['state'] or '-':16} {turn:6} {report}")
        if row["status"]:
            print(f"    {row['status']}")


def main(argv):
    if not argv or any(a in ("-h", "--help", "help") for a in argv):
        print(__doc__.strip())
        return
    cmd, rest = argv[0], argv[1:]
    if cmd == "render":
        values = dict(kv.split("=", 1) for kv in rest[2:])
        missing = [k for k in PLACEHOLDERS if k not in values]
        if missing:
            raise SystemExit(f"render: missing {' '.join(missing)}")
        render(rest[0], rest[1], values)
    elif cmd == "classify":
        print(classify(rest[0], rest[1], sys.stdin.read()))
    elif cmd == "delivered":
        print(delivered(rest[0], rest[1], sys.stdin.read()))
    elif cmd == "unfinished":
        left = unfinished(rest[0])
        if left:
            print(", ".join(left))
    elif cmd == "status":
        if not (len(rest) == 1 or len(rest) == 2 and rest[1] == "--json"):
            print("fleet_orch: usage: status <state-dir> [--json]", file=sys.stderr)
            raise SystemExit(2)
        print_status(rest[0], "--json" in rest[1:])
    elif cmd == "pick":
        try:
            v = pick(json.load(sys.stdin), rest[0], *rest[1:3])
        except (KeyError, StopIteration, TypeError, ValueError, IndexError) as e:
            raise SystemExit(f"pick: no {' '.join(rest)} ({e.__class__.__name__})")
        print(v if isinstance(v, str) else json.dumps(v))
    else:
        raise SystemExit(f"fleet_orch: unknown command {cmd}")


if __name__ == "__main__":
    main(sys.argv[1:])
