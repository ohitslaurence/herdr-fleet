"""fleet gripe — agents log the friction they hit, so recurring pain gets fixed.

  fleet gripe "<what went wrong>" [--tried "<what you did>"] [--cost 10m] [--fix "<what would help>"]
              [--about <tool, doc or file>]
  fleet gripes [--since 7d] [--all] [--json]

One line per gripe in ~/.local/state/fleet/gripes.jsonl (owner-only, on this machine, never in a
repo: gripes quote paths, errors and private code). Text is redacted before it's written. Context
comes from the environment: pane, agent kind, repo, branch and the orch worker name.
`fleet gripes` lists them grouped by what they're about, newest first; reading it as a human
(no --json) marks them seen, and the cockpit counts the unseen ones.
"""
import json, os, re, subprocess, sys
from datetime import datetime, timedelta, timezone

import fleet_redact, fleet_state

STATE_DIR = os.path.expanduser("~/.local/state/fleet")
LOG = os.path.join(STATE_DIR, "gripes.jsonl")
SEEN = os.path.join(STATE_DIR, "gripes-seen.json")
MAX_BYTES = 2_000_000
MAX_TEXT = 600
FIELDS = ("tried", "cost", "fix", "about")
USAGE = ('usage: fleet gripe "<what went wrong>" [--tried "<...>"] [--cost 10m] [--fix "<...>"] '
         '[--about <tool|doc|file>]\n       fleet gripes [--since 7d] [--all] [--json]')


def now():
    return datetime.now(timezone.utc)


def _git(cwd, *args):
    try:
        p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def repo_of(cwd):
    """owner/name from origin, else the folder name; None outside a repo."""
    url = _git(cwd, "remote", "get-url", "origin")
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
    if m:
        return m.group(1)
    top = _git(cwd, "rev-parse", "--show-toplevel")
    return os.path.basename(top) if top else None


def agent_kind(env):
    if env.get("CLAUDECODE"):
        return "claude"
    if any(k.startswith("CODEX_") for k in env):
        return "codex"
    if env.get("OPENCODE") or any(k.startswith("OPENCODE_") for k in env):
        return "opencode"
    return None


def worker_of(pane):
    """The orch worker running in this pane, if any."""
    if not pane:
        return None
    import fleet_orch
    for directory, meta, _ in fleet_orch.workers(fleet_orch.home()):
        if meta.get("pane") == pane:
            return directory.name
    return None


def clean(text):
    return fleet_redact.redact(" ".join(text.split()))[:MAX_TEXT]


def parse(argv):
    text, extra, i = [], {}, 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--") and a[2:] in FIELDS:
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                raise ValueError(f"{a} needs a value")
            extra[a[2:]] = argv[i + 1]
            i += 2
        elif a.startswith("-"):
            raise ValueError(f"unknown option {a}")
        else:
            text.append(a)
            i += 1
    if not " ".join(text).strip():
        raise ValueError("say what went wrong")
    return " ".join(text), extra


def gripe(argv, env=None, cwd=None):
    env = os.environ if env is None else env
    cwd = cwd or os.getcwd()
    text, extra = parse(argv)
    pane = env.get("HERDR_PANE_ID")
    entry = {"at": now().isoformat(timespec="seconds"), "text": clean(text),
             **{k: clean(v) for k, v in extra.items()},
             "kind": agent_kind(env), "pane": pane, "worker": worker_of(pane),
             "repo": repo_of(cwd), "branch": _git(cwd, "branch", "--show-current") or None, "cwd": cwd}
    fleet_state.append_jsonl(LOG, {k: v for k, v in entry.items() if v}, MAX_BYTES)
    return entry


def read_all():
    out = []
    for path in (LOG + ".1", LOG):
        try:
            with open(path) as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except FileNotFoundError:
            continue
    return out


def seen_at():
    return fleet_state.read(SEEN, {}).get("at", "")


def unseen():
    """How many gripes came in since a human last read `fleet gripes`."""
    seen = seen_at()
    return sum(g.get("at", "") > seen for g in read_all())


def since(spec):
    m = re.fullmatch(r"(\d+)([hdw])", spec or "")
    if not m:
        raise ValueError(f"--since takes 12h, 7d or 2w, not {spec!r}")
    n, unit = int(m.group(1)), m.group(2)
    return now() - timedelta(**{{"h": "hours", "d": "days", "w": "weeks"}[unit]: n})


def topic(g):
    return g.get("about") or g.get("repo") or "(no repo)"


def render(gripes, seen):
    groups = {}
    for g in sorted(gripes, key=lambda g: g.get("at", ""), reverse=True):
        groups.setdefault(topic(g), []).append(g)
    lines = []
    for name, gs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        lines.append(f"{name} ({len(gs)})")
        for g in gs:
            who = "/".join(x for x in (g.get("kind"), g.get("worker")) if x) or "?"
            new = "*" if g.get("at", "") > seen else " "
            lines.append(f" {new} {g.get('at', '')[:16].replace('T', ' ')}  {who}: {g['text']}")
            for k, label in (("tried", "tried"), ("cost", "cost"), ("fix", "would help")):
                if g.get(k):
                    lines.append(f"      {label}: {g[k]}")
    return "\n".join(lines)


def gripes(argv):
    as_json = "--json" in argv
    rest = [a for a in argv if a != "--json"]
    cutoff = None
    if "--all" in rest:
        rest.remove("--all")
    else:
        spec = "7d"
        if "--since" in rest:
            i = rest.index("--since")
            spec = rest[i + 1] if i + 1 < len(rest) else ""
            del rest[i:i + 2]
        cutoff = since(spec).isoformat(timespec="seconds")
    if rest:
        raise ValueError(f"unexpected {' '.join(rest)}")
    got = [g for g in read_all() if cutoff is None or g.get("at", "") >= cutoff]
    if as_json:
        print(json.dumps({"gripes": got, "unseen": unseen()}))
        return
    print(render(got, seen_at()) or "no gripes" + ("" if cutoff is None else " in that window (--all for every one)"))
    fleet_state.update(SEEN, lambda d: d.__setitem__("at", now().isoformat(timespec="seconds")), {})


def main(argv):
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "gripe":
            e = gripe(rest)
            print(f"logged, thanks: {e['text']}  (read them with `fleet gripes`)")
        else:
            gripes(rest)
    except ValueError as e:
        print(f"fleet {cmd}: {e}\n{USAGE}", file=sys.stderr)
        return 2
    return 0
