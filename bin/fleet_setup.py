"""fleet_setup — propose a first config from what's on this machine; write it only for a human.

  fleet setup --propose [--json]   read-only: look around, print the config it would write
  fleet setup apply <file|->       validate, show the diff, ask on the terminal, back up, write
  fleet setup                      both, asking about each choice (needs a terminal)

Proposals never loosen safety: permissions "bypass", merge "auto", auto-replies and admin
merges only ever come from the human. With a config already there, it only adds settings the
file doesn't have yet; it never rewrites what you chose.
"""
import difflib, json, os, re, shutil, subprocess, sys, tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import fleet_config  # first: it checks for Python 3.11+
import tomllib

CANDIDATE_DIRS = ("~/dev", "~/code", "~/src", "~/projects", "~/work", "~/git", "~/repos")
AGENTS = ("claude", "codex", "opencode")
NEVER = {("agents", "permissions"): "bypass", ("ship", "merge"): "auto",
         ("watch", "auto_reply"): True, ("ship", "admin"): True}


def sh(*args, cwd=None):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=15, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def checkouts(d):
    """Direct children with a .git directory: how dev counts repos."""
    d = os.path.expanduser(d)
    try:
        return sorted(e.path for e in os.scandir(d) if os.path.isdir(os.path.join(e.path, ".git")))
    except OSError:
        return []


def inspect():
    """(proposed {section: {key: value}}, evidence {dotted: why}, ask [dotted])."""
    proposed, evidence, ask = {}, {}, []

    def propose(dotted, value, why, asking=False):
        section, key = dotted.split(".", 1)
        if NEVER.get((section, key)) == value:
            return
        proposed.setdefault(section, {})[key] = value
        evidence[dotted] = why
        if asking:
            ask.append(dotted)

    dirs = list(CANDIDATE_DIRS) + ([ghq] if (ghq := sh("ghq", "root")) else [])
    for d in list(dirs):  # one level down too: ~/dev/<org>/ is a common layout
        try:
            dirs += [e.path for e in os.scandir(os.path.expanduser(d))
                     if e.is_dir() and not os.path.exists(os.path.join(e.path, ".git"))]
        except OSError:
            pass
    counts = {d: len(checkouts(d)) for d in dict.fromkeys(dirs)}
    home = os.path.expanduser("~")
    counts = {("~" + d[len(home):] if d.startswith(home + "/") else d): c for d, c in counts.items()}
    ranked = sorted((c, d) for d, c in counts.items() if c)[::-1]
    if ranked:
        propose("dev.repos_dir", ranked[0][1],
                "; ".join(f"{d} has {c} git checkout(s)" for c, d in ranked[:3]), asking=len(ranked) > 1)
    else:
        evidence["dev.repos_dir"] = f"no git checkouts in {', '.join(CANDIDATE_DIRS)}"
        ask.append("dev.repos_dir")
    repos = checkouts(ranked[0][1]) if ranked else []

    if name := sh("git", "config", "--global", "user.name"):
        propose("user.name", name, "git config user.name")
    if login := sh("gh", "api", "user", "--jq", ".login"):
        propose("user.github", login, "gh's login")

    owners = Counter()
    for path in repos:
        url = sh("git", "-C", path, "remote", "get-url", "origin")
        if m := re.search(r"github\.com[:/]([^/]+)/", url):
            owners[m.group(1)] += 1
    member = set(sh("gh", "api", "user/orgs", "--jq", ".[].login").split())
    orgs = [o for o, _ in owners.most_common() if o in member or o == login][:3]
    if orgs:
        propose("github.orgs", orgs, "owners of origin in your repos: " +
                ", ".join(f"{o} ({n})" for o, n in owners.most_common(5)), asking=True)

    shapes = Counter()
    for path in repos[:15]:
        out = sh("git", "-C", path, "worktree", "list", "--porcelain")
        for wt in re.findall(r"^worktree (.+)$", out, re.M)[1:]:
            shapes["nested" if wt.startswith(path + "/") else "sibling"] += 1
    if shapes and shapes.most_common(1)[0][0] == "nested":
        propose("worktree.path", "{repo_path}/.worktrees/{task}", f"{shapes['nested']} of your worktrees live inside the repo")

    prefixes = Counter()
    for path in repos[:15]:
        for b in sh("git", "-C", path, "for-each-ref", "--count=50", "--format=%(refname:short)", "refs/heads").split():
            if "/" in b:
                prefixes[b.split("/", 1)[0]] += 1
    import fleet_paths
    if prefixes:
        top, n = prefixes.most_common(1)[0]
        if n >= 10 and top in (fleet_paths._user(), login):
            propose("branch.template", "{user}/{task}" if top == fleet_paths._user() else f"{top}/{{task}}",
                    f"{n} of your recent branches start with {top}/")

    on_path = [a for a in AGENTS if shutil.which(a)]
    if on_path:
        evidence["roles.driver"] = f"agent CLIs on PATH: {', '.join(on_path)}; fleet new starts the driver"
        if on_path[0] != fleet_config.DEFAULTS["roles"]["driver"]["kind"]:
            propose("roles.driver", {"kind": on_path[0]}, evidence["roles.driver"])
        if len(on_path) > 1:
            ask.append("roles.driver")
    else:
        evidence["roles.driver"] = "no agent CLI on PATH (claude, codex, opencode)"
    ask.append("agents.permissions")
    evidence["agents.permissions"] = ('"ask": agents stop for approval on tools (safe, slower); "bypass": no checks, '
                                      "every agent runs as you. Only you can choose bypass.")
    if repos:
        ask.append("spares.repos")
        evidence["spares.repos"] = "a ready worktree per listed repo makes fleet new start in ~1 s; each costs a checkout + deps"
    return proposed, evidence, ask


def dump(tree):
    """TOML for {section: {key: value | {table}}}: sections, then nested tables."""
    lines = []
    for section, body in tree.items():
        flat = {k: v for k, v in body.items() if not isinstance(v, dict)}
        if flat:
            lines.append(f"[{section}]")
            lines += [f"{fleet_config._key(k)} = {fleet_config._val(v)}" for k, v in flat.items()]
            lines.append("")
        for k, v in body.items():
            if isinstance(v, dict):
                lines.append(f"[{section}.{fleet_config._key(k)}]")
                lines += [f"{fleet_config._key(x)} = {fleet_config._val(y)}" for x, y in v.items()]
                lines.append("")
    return "\n".join(lines)


def propose():
    """The full proposal: only what the current file doesn't set already."""
    current_text = Path(fleet_config.PATH).read_text() if os.path.exists(fleet_config.PATH) else ""
    current = tomllib.loads(current_text) if current_text else {}
    proposed, evidence, ask = inspect()
    new = {}
    for section, body in proposed.items():
        for k, v in body.items():
            if k not in current.get(section, {}):
                new.setdefault(section, {})[k] = v
    keep = [a for a in ask if a.split(".", 1)[1] not in current.get(a.split(".", 1)[0], {})]
    addition = dump(new)
    text = (current_text.rstrip() + "\n\n" + addition if current_text.strip() else addition) if addition else current_text
    diff = "".join(difflib.unified_diff(current_text.splitlines(True), text.splitlines(True),
                                        fleet_config.PATH, fleet_config.PATH + " (proposed)"))
    return {"config_path": fleet_config.PATH, "exists": bool(current_text), "proposed": new,
            "evidence": evidence, "ask": keep, "toml": text, "diff": diff,
            "never_proposed": sorted(f"{s}.{k}" for s, k in NEVER)}


def apply(text, tty=None, source=None):
    """Validate first (anyone may learn their file is wrong), then write only for a terminal."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        print(f"fleet setup: not valid TOML: {e}", file=sys.stderr)
        return 1
    problems = fleet_config.validate(data)
    for p in problems:
        print(f"{p['level']}: {p['key']}: {p['msg']}", file=sys.stderr)
    if any(p["level"] == "fail" for p in problems):
        return 1
    current = Path(fleet_config.PATH).read_text() if os.path.exists(fleet_config.PATH) else ""
    diff = "".join(difflib.unified_diff(current.splitlines(True), text.splitlines(True),
                                        fleet_config.PATH, "new"))
    if not diff:
        print("no changes")
        return 0
    import fleet_term
    tty = tty or fleet_term.need_terminal("fleet setup", rerun=f"fleet setup apply {source}" if source else None)
    print(diff)
    tty.write(f"write {fleet_config.PATH}? [y/N] ")
    tty.flush()
    if tty.readline().strip().lower() not in ("y", "yes"):
        print("not written")
        return 3
    os.makedirs(fleet_config.DIR, exist_ok=True)
    if current:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(fleet_config.PATH, f"{fleet_config.PATH}.bak-{stamp}")
    target = os.path.realpath(fleet_config.PATH)  # keep a symlinked config (dotfiles) a symlink
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".config.toml.")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    print(f"wrote {fleet_config.PATH}")
    return 0


def wizard(tty):
    p = propose()
    for k, why in p["evidence"].items():
        print(f"  {k}: {why}")
    tree = p["proposed"]
    for dotted in p["ask"]:
        section, key = dotted.split(".", 1)
        cur = tree.get(section, {}).get(key)
        tty.write(f"\n{dotted} [{fleet_config._val(cur) if cur is not None else 'default'}] (enter keeps it): ")
        tty.flush()
        got = tty.readline().strip()
        if not got:
            continue
        try:
            value = tomllib.loads(f"v = {got}")["v"]
        except tomllib.TOMLDecodeError:
            value = got  # a bare word is a string
        tree.setdefault(section, {})[key] = value
    current = Path(fleet_config.PATH).read_text() if os.path.exists(fleet_config.PATH) else ""
    return apply((current.rstrip() + "\n\n" if current.strip() else "") + dump(tree), tty)


def main(argv):
    if "--propose" in argv:
        extra = next((a for a in argv if a not in ("--propose", "--json")), None)
        if extra:
            print(f"fleet setup: unexpected {extra!r}; usage: fleet setup --propose [--json]", file=sys.stderr)
            return 2
        p = propose()
        if "--json" in argv:
            print(json.dumps(p, indent=1))
        else:
            for k, why in p["evidence"].items():
                print(f"{'ask ' if k in p['ask'] else '    '}{k}: {why}")
            print("\n" + (p["diff"] or "nothing to add: your config already sets all of these"))
        return 0
    if argv[:1] == ["apply"]:
        if len(argv) < 2:
            print("usage: fleet setup apply <file|->", file=sys.stderr)
            return 2
        text = sys.stdin.read() if argv[1] == "-" else Path(argv[1]).read_text()
        return apply(text, source=None if argv[1] == "-" else os.path.abspath(argv[1]))
    if argv:
        print(f"fleet setup: unknown argument {argv[0]!r}; see fleet setup --help", file=sys.stderr)
        return 2
    import fleet_term
    return wizard(fleet_term.need_terminal("fleet setup"))
