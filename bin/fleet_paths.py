"""fleet_paths — where worktrees live and what branches are called, in one place.

Every tool used to build `<repo-path>-wt-<task>` and `<repo>__<task>` by hand, and several
detected a worktree by finding `-wt-` in its name. Now the shapes are templates,
read from ~/.config/fleet/config.toml, defaulting to exactly the old shapes:

    [worktree]
    path    = "{repo_path}-wt-{task}"        # also: "{repo_path}/.worktrees/{task}", "~/wt/{repo}/{task}"
    pr_path = "{repo_path}-wt-pr-{number}"
    [branch]
    template = "{repo}__{task}"              # also: "{user}/{task}"

Placeholders: {repo_path} (canonical checkout), {repo} (alias), {task}, {number}, {user}.

Two different questions, kept apart on purpose:
  dev_owned(path, ...)    did `dev` create this checkout (it matches the template)? Used by prune
                          before deleting anything: spares and review checkouts are linked
                          worktrees too, and must never match.
  is_linked_worktree(p)   is this any git linked worktree? Used where "an agent's own checkout,
                          not a canonical clone" is the question (fleet watch, cockpit).

CLI (for shell tools like orch):
  fleet_paths.py worktree <repo_path> <alias> <task>
  fleet_paths.py branch <alias> <task>
"""
import getpass, os, re, shutil, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet_config  # noqa: E402

# git args for making or switching a worktree. A repo's checkout hooks (husky's `yarn yarnhook`)
# need dependencies a fresh worktree doesn't have yet, and fail it: git exits 1 with the checkout
# made. Every tool's own install step does what such a hook wanted. (orch spells it out in bash.)
NO_HOOKS = ("-c", "core.hooksPath=/dev/null")

DEFAULTS = {"path": "{repo_path}-wt-{task}", "pr_path": "{repo_path}-wt-pr-{number}", "branch": "{repo}__{task}"}
_cache = {}


def templates():
    if "t" not in _cache:
        _cache["t"] = {"path": fleet_config.get("worktree", "path", DEFAULTS["path"]),
                       "pr_path": fleet_config.get("worktree", "pr_path", DEFAULTS["pr_path"]),
                       "branch": fleet_config.get("branch", "template", DEFAULTS["branch"])}
    return _cache["t"]


def _user():
    return os.environ.get("FLEET_USER") or getpass.getuser()


def _fill(template, **kw):
    kw.setdefault("user", _user())
    return template.format(**kw)


def worktree_path(repo_path, alias, task):
    return os.path.expanduser(_fill(templates()["path"], repo_path=repo_path.rstrip("/"), repo=alias, task=task))


def pr_worktree_path(repo_path, alias, number):
    return os.path.expanduser(_fill(templates()["pr_path"], repo_path=repo_path.rstrip("/"), repo=alias,
                                    number=number))


def branch_name(alias, task):
    return _fill(templates()["branch"], repo=alias, task=task)


def _pattern(template, **fixed):
    """Template -> regex: fixed placeholders become literals, {task} a path segment, {number} digits."""
    fixed.setdefault("user", _user())
    out, pos = "", 0
    for m in re.finditer(r"\{(\w+)\}", template):
        out += re.escape(template[pos:m.start()])
        name = m.group(1)
        if name in fixed:
            out += re.escape(str(fixed[name]))
        elif name == "number":
            out += r"(?P<number>\d+)"
        else:
            out += rf"(?P<{name}>[^/]+)"
        pos = m.end()
    return "^" + out + re.escape(template[pos:]) + "$"


def task_of(path, repo_path, alias):
    """The task a dev-made checkout belongs to, or None if it doesn't match the template."""
    path = os.path.realpath(os.path.expanduser(path))
    for key in ("path", "pr_path"):
        tpl = os.path.expanduser(templates()[key])
        m = re.match(_pattern(tpl, repo_path=os.path.realpath(repo_path), repo=alias), path)
        if m:
            return m.groupdict().get("task") or f"pr-{m.group('number')}"
    return None


def dev_owned(path, repo_path, alias):
    return task_of(path, repo_path, alias) is not None


def branch_task(branch, alias):
    """Inverse of branch_name: the task if `branch` is a dev branch of this repo, else None."""
    m = re.match(_pattern(templates()["branch"], repo=alias), branch or "")
    return m.group("task") if m and "task" in m.groupdict() else None


def count_worktrees(repo_path, alias):
    """How many dev worktrees this repo has (fleet new uses it as a "most used" hint). The
    default sibling layout is a directory listing; other layouts ask git."""
    tpl = templates()["path"]
    if tpl.startswith("{repo_path}-") and tpl.endswith("{task}") and tpl.count("{") == 2:
        prefix = os.path.basename(repo_path.rstrip("/")) + tpl[len("{repo_path}"):-len("{task}")]
        try:
            return sum(1 for n in os.listdir(os.path.dirname(repo_path.rstrip("/"))) if n.startswith(prefix))
        except OSError:
            return 0
    return sum(1 for wt in worktrees(repo_path) if dev_owned(wt, repo_path, alias))


def worktrees(repo_path):
    """Every checkout of the repo at repo_path (any of its worktrees will do), from `git worktree
    list`; [] when git can't say."""
    try:
        out = subprocess.run(["git", "-C", repo_path, "worktree", "list", "--porcelain"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line[9:] for line in out.splitlines() if line.startswith("worktree ")]


def head_branch(cwd):
    """The checked-out branch at cwd ("HEAD" when detached), read from git's files: no process.
    None when cwd isn't in a git checkout. Handles linked worktrees (.git is a file)."""
    d = os.path.abspath(cwd)
    while True:
        dot = os.path.join(d, ".git")
        if os.path.exists(dot):
            break
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    try:
        if os.path.isfile(dot):
            with open(dot) as f:
                line = f.read().strip()
            if not line.startswith("gitdir:"):
                return None
            gitdir = os.path.join(d, line[len("gitdir:"):].strip())
        else:
            gitdir = dot
        with open(os.path.join(gitdir, "HEAD")) as f:
            head = f.read().strip()
    except OSError:
        return None
    return head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else "HEAD"


def is_linked_worktree(path):
    """True for a git linked worktree (its git dir differs from the common dir); False for a
    canonical clone, a non-repo, or anything git can't read."""
    try:
        p = subprocess.run(["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-dir",
                            "--git-common-dir"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    lines = p.stdout.split()
    return p.returncode == 0 and len(lines) == 2 and os.path.realpath(lines[0]) != os.path.realpath(lines[1])


def warm_caches(path, replace=False):
    """Copy each [dev] warm_caches dir (a gitignored build cache such as a tsc buildinfo) into the
    new checkout at `path` from whichever other worktree of the repo wrote it last, so its first
    build is incremental instead of cold. A cold typecheck can take 5x the memory of a warm one,
    and a fleet starting a dozen at once runs the machine out. The caches validate their own
    inputs, so one from a checkout on other code costs a partial rebuild, never a wrong result.
    Dirs `path` already has are left alone, unless `replace` and another worktree has written
    the dir since: a spare never builds, so it re-copies on every refresh. Copies keep their
    mtimes, so a copy never looks newer than its source. Returns "<dir> from <checkout>" per copy."""
    rels = []
    for r in fleet_config.get("dev", "warm_caches", []):
        if not r or os.path.isabs(r) or ".." in r.split("/"):
            print(f"fleet: ignoring [dev] warm_caches entry {r!r}: it must be a path inside the repo",
                  file=sys.stderr)
        else:
            rels.append(r)
    if not rels:
        return []
    others = [o for o in worktrees(path) if os.path.realpath(o) != os.path.realpath(path)]
    copied = []
    for rel in rels:
        dst = os.path.join(path, rel)
        if os.path.exists(dst) and not replace:
            continue
        written, src = max(((_newest_mtime(os.path.join(o, rel)), o) for o in others), default=(0, ""))
        if written <= _newest_mtime(dst):
            continue
        shutil.rmtree(dst, ignore_errors=True)
        try:
            shutil.copytree(os.path.join(src, rel), dst, symlinks=True)
        except (OSError, shutil.Error):
            shutil.rmtree(dst, ignore_errors=True)  # a half-copied cache is worse than none
            continue
        copied.append(f"{rel} from {src}")
    return copied


def _newest_mtime(d):
    """Latest mtime of any file under d; 0 when d is missing or empty."""
    newest = 0
    for root, _, files in os.walk(d):
        for name in files:
            try:
                newest = max(newest, os.stat(os.path.join(root, name)).st_mtime)
            except OSError:
                pass
    return newest


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["worktree"] and len(a) == 4:
        print(worktree_path(a[1], a[2], a[3]))
    elif a[:1] == ["branch"] and len(a) == 3:
        print(branch_name(a[1], a[2]))
    else:
        sys.exit(__doc__.strip().split("CLI")[1].strip())
