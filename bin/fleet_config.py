#!/usr/bin/env python3
"""fleet_config — every fleet setting, from layers, with the source of each value.

Layers, later wins (`fleet config show` prints each value and the layer that set it):

  default    built into this file; generic and safe for a stranger
  preset     a team file: `preset = "<path>"` in config.toml. Its `[lock] keys = [...]`
             can't be changed by later layers.
  personal   ~/.config/fleet/config.toml
  repo       <repo>/.fleet.toml on the PR's base branch, read by ship per PR. Tighten-only:
             see tighten().

No environment variable can point at another file: agents share our environment and must not
be able to load a looser policy. Lists in ADDITIVE are unions (no layer removes an entry), so
protected paths and veto words only ever grow. A broken file is an error, not silently skipped.

    [user]      name = "Ada"; github = "ada"        (github default: gh's login)
    [github]    orgs = ["acme"]
    [worktree]  path = "{repo_path}-wt-{task}"; pr_path = "{repo_path}-wt-pr-{number}"
    [branch]    template = "{repo}__{task}"
    [ship]      merge, admin, method, review, require_selfreview, cross_review, ... ; [ship.tiers.*]
    [protect]   paths = ["*payment*", ...]           (was human_paths)
    [repos.<name>]  per-repo [ship] overrides, plus protect_paths
    [agents]    permissions = "ask" | "bypass"      (how fleet new starts agents)
    [watch]     interval, auto_reply, ..., human_words
    [decisions] "<point>" = "shadow" | "act" | "off"
    [hints]     <repo> = "what lives there"         (fleet new)
    [spares]    repos = [...]
    [roles.<role>] kind/model/effort    (roles.driver.kind: the agent fleet new starts by default)
    [kinds.<kind>] cmd, bypass, model, model_flag, about, names, models, models_cmd   (agent harnesses)
    [models.<nick>] harness, model, names     (your names for models: "use kimi")

CLI: fleet config show [key] [--repo owner/name[@base]] [--json]
     fleet config get <section.key>     the bare value, for shell tools (orch)
"""
import copy, json, os, re, subprocess, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet_py  # noqa: E402,F401  (Python 3.11+, before tomllib)
import tomllib  # noqa: E402
from pathlib import Path

DIR = os.path.expanduser("~/.config/fleet")
PATH = os.path.join(DIR, "config.toml")
STATE_DIR = os.path.expanduser("~/.local/state/fleet")
ADDITIVE = re.compile(r"protect\.paths|watch\.human_words|repos\.[^.]+\.protect_paths")

DEFAULTS = {
    "user": {"name": "the user", "github": ""},  # github "": gh's login
    "github": {"orgs": []},
    "worktree": {"path": "{repo_path}-wt-{task}", "pr_path": "{repo_path}-wt-pr-{number}"},
    "branch": {"template": "{repo}__{task}"},
    "ship": {
        "merge": "ask",              # ask: stop at awaiting-human | auto: merge clean PRs off protected paths
        "admin": False,              # allow `gh pr merge --admin` past the required approval
        "method": "squash",
        "finding_bots": [],          # other review bots whose inline findings block (login substrings)
        "review": "greptile",        # review bot to wait on (fleet_review.PROVIDERS) or "none"; auto-off
                                     # on repos the bot has never reviewed
        # Paid re-reviews are budgeted per risk tier; "critical" = touches protected paths, the
        # review bot says Critical impact, or the repo forces it (repos.<name>.tier).
        "tiers": {
            "normal": {"min_score": 4, "max_rereviews": 1, "fixup_max_lines": 80, "free_review_first": False},
            "critical": {"min_score": 5, "max_rereviews": 3, "fixup_max_lines": 15, "free_review_first": True},
        },
        "require_selfreview": True,  # a re-tag needs a self-review comment on the head first
        "cross_review": False,       # a free cross-family review on every PR, not just on escalation
        "max_free_reviews": 4,
        "craft_review": False,       # a quiet craft review on the draft before anyone else reviews it
        # timing and thresholds (minutes unless named otherwise); per repo via [repos.<name>]
        "ci_grace_min": 3,            # a head this new with no checks yet: CI hasn't registered
        "required_start_min": 30,     # a required check still absent this long won't start
        "review_timeout_min": 45,     # a review request with no verdict this long counts as dropped
        "review_first_grace_min": 5,  # a new PR this young without a review: the bot is still coming
        "deploy_start_min": 5,        # post-merge runs missing this long after a merge aren't coming
        "deploy_timeout_min": 90,     # a deploy run still going this long needs a look
        "deploy_history_pushes": 5,   # "always runs on merge" = ran on each of the last N pushes
        "deploy_lookback_hours": 72,  # ship pr doesn't ask about deploys of older merges
        "split_pr_files": 15,         # the stuck report suggests splitting PRs bigger than this
        "large_pr_files": 40,         # ...and calls them too big to converge past this
        "deploy": "actions",         # what to watch after a merge (fleet_deploy): actions | none
        "after_deploy": "",          # shell command `ship --act` runs once when a PR is deployed
        "checks": "required",        # required: only checks the base branch requires gate (all of them
                                     # when it requires none) | all: every check gates
    },
    "protect": {"paths": []},
    "watch": {"interval": 120, "auto_reply": False, "blocked_minutes": 10, "merge_lookback_hours": 6,
              "ntfy_topic": "", "refresh_minutes": 10, "human_words": [],
              "event_settle_s": 3,        # after an agent event, wait this long for its neighbours
              "event_min_gap_s": 15,      # event-woken passes at most this often (polling covers the rest)
              "auto_reply_text": "",      # what an auto-reply says ("" = the built-in wording)
              "inbox_max_open": 100, "inbox_open_days": 3, "inbox_keep_days": 7, "inbox_max_items": 500,
              "craft_keep_days": 14},     # then craft findings keep only summary and verdict
    # Safe for someone who hasn't chosen otherwise: agents ask before acting, the watch only
    # reports offers (auto_reply above), and no decision point calls a model.
    "agents": {"permissions": "ask",  # ask | bypass (claude --dangerously-skip-permissions, ...)
               "opposite": ["claude", "codex"],  # roles with kind = "opposite" pick the other of these
               "command_timeout_s": 90},  # a gh/git/herdr call that hangs longer counts as failed
    "decisions": {"default": "off",
                  "model": "jev-latest"},  # the TypeSafe model fleet new and decision points ask
    "hints": {},
    "spares": {"repos": [], "refresh_minutes": 20},
    # dependency install for a new worktree: the first lockfile found picks the command
    "setup": {"installs": [["bun.lock", "bun install"], ["bun.lockb", "bun install"],
                           ["pnpm-lock.yaml", "pnpm install --frozen-lockfile"], ["yarn.lock", "yarn install"],
                           ["package-lock.json", "npm ci"]]},
    # repos dev and fleet new know: every git checkout directly in repos_dir (alias = folder
    # name), plus [dev.repos.<alias>] path = "...", base = "main" for others or to override one
    "dev": {"repos_dir": "", "repos": {}, "warm_caches": []},
    # fleet remote: read-only access for an agent on your tailnet (docs/remote.md). scopes:
    # "tag:<name>" = ["fleet_status", "fleet_overview", "fleet_inbox"]; empty refuses everyone
    "remote": {"port": 7412, "rate_per_min": 30, "scopes": {}},
    "layout": {"tabs": ["shell", "agent"]},  # tabs a new session opens with (dev); "agent" runs the agent
    "cockpit": {"stale_days": 7,            # open PRs with no agent and no push this long go to BACKLOG
                "urgency_ttl_hours": 6,     # how long an urgency score is reused for an unchanged row
                "recheck_minutes": 60},     # re-run ship on an unchanged PR at least this often
    "new": {"classifier": "auto",           # auto (first with a key: jev, decisions, then haiku) | jev | decisions | haiku
            "duplicate_min": 0.5,           # probability of "same as an open session" before offering to jump
            "send_sentence": False},        # start the agent on your sentence (else it waits for you)
    "roles": {"driver": {"kind": "claude"}, "worker": {"kind": "codex"},
              "reviewer": {"kind": "opposite"}, "advisor": {"kind": "claude"},
              "security": {"kind": "claude"}},  # orch start --role security: Codex's cyber filter refuses these briefs
    "orch": {"quiet_min": 5},  # a worker idle this long with no status line: orch wait reports it (0 = never)
    # Agent harnesses fleet new can start (the program, not the model). Built-ins know only their
    # own command, flags and how to list models; add others as [kinds.<name>]. `names` are words
    # that name the harness itself. Models are separate: each harness's list, plus your own
    # [models.<nickname>] (e.g. harness = "opencode", model = "opencode/kimi-k3").
    "kinds": {
        "claude": {"cmd": ["claude"], "bypass": ["--dangerously-skip-permissions"], "model_flag": "--model",
                   "system_flag": "--append-system-prompt",  # how fleet new tells an idle agent about setup
                   "about": "Claude Code", "names": ["claude", "claude code"],
                   "models": ["fable", "opus", "sonnet", "haiku"]},  # aliases `claude --model` documents
        "codex": {"cmd": ["codex"], "bypass": ["--dangerously-bypass-approvals-and-sandbox"],
                  "model_flag": "--model", "about": "Codex CLI", "names": ["codex"]},
        "opencode": {"cmd": ["opencode"], "bypass": ["--auto"], "model_flag": "--model",
                     "about": "opencode (runs models from many providers)", "names": ["opencode"],
                     "models_cmd": ["opencode", "models"]},
    },
    "models": {},  # [models.<nickname>] harness, model, names: your words for models ("kimi")
    "repos": {},
}
_cache = {}


# ── layers ───────────────────────────────────────────────────────────────────

class ConfigError(RuntimeError):
    """The config file is missing or isn't valid TOML: print it, don't trace it."""


def _config_error_hook(kind, err, tb, _default=sys.excepthook):
    """Every tool reads the config, often at import: a typo there gets one line, not a trace."""
    if issubclass(kind, ConfigError):
        print(f"fleet: {err}", file=sys.stderr)
    else:
        _default(kind, err, tb)


sys.excepthook = _config_error_hook


def _toml(path, required=False):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        if required:
            raise ConfigError(f"{path} not found")
        return {}
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}")


class Resolved:
    def __init__(self):
        self.tree, self.sources, self.ignored, self.layers = {}, {}, [], []

    def merge(self, data, layer, locked=()):
        self.layers.append(layer)
        self._merge(self.tree, data, layer, "", "", locked)

    def _merge(self, tree, data, layer, prefix, qprefix, locked):
        """`key` is the raw dotted path (locks, ADDITIVE); `qkey` quotes parts that contain dots
        ("cockpit.urgency") and is how sources, ignored entries and `show` name a value."""
        for k, v in data.items():
            key, qkey = prefix + k, qprefix + _key(k)
            if any(key == lk or key.startswith(lk + ".") for lk in locked):
                self.ignored.append((qkey, layer, "locked by the preset"))
            elif isinstance(v, dict):
                if not isinstance(tree.setdefault(k, {}), dict):
                    self.ignored.append((qkey, layer, "expected a value, not a table"))
                    continue
                self._merge(tree[k], v, layer, key + ".", qkey + ".", locked)
            elif isinstance(tree.get(k), dict):
                self.ignored.append((qkey, layer, "expected a table"))
            elif ADDITIVE.fullmatch(key):
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    self.ignored.append((qkey, layer, "expected a list of strings"))
                    continue
                cur = tree.get(k, [])
                tree[k] = cur + [x for x in v if x not in cur]
                if v and layer not in self.sources.get(qkey, "").split("+"):
                    self.sources[qkey] = "+".join(filter(None, [self.sources.get(qkey), layer]))
            else:
                tree[k] = copy.deepcopy(v)
                self.sources[qkey] = layer


def build():
    """The config as the files say now, without touching what readers are using."""
    r = Resolved()
    r.merge(DEFAULTS, "default")
    personal = _toml(PATH)
    locked = ()
    if preset := personal.pop("preset", None):
        p = _toml(os.path.expanduser(preset), required=True)
        locked = tuple(p.pop("lock", {}).get("keys", []))
        r.merge(p, "preset")
    r.merge(personal, "personal", locked)
    return r


def resolve():
    if "r" not in _cache:
        _cache["r"] = build()
    return _cache["r"]


def reload(check=None):
    """Re-read the files and swap the result in with one store, so a reader sees the old config or
    the new one, never neither. Raises (and keeps the old) if they don't parse or check(tree) does."""
    r = build()
    if check:
        check(r.tree)
    _cache["r"] = r


def load():
    return resolve().tree


def get(section, key, default=None):
    v = load().get(section, {})
    return v.get(key, default) if isinstance(v, dict) else default


# ── values the tools ask for ─────────────────────────────────────────────────

def user_name():
    return get("user", "name") or "the user"


def github_login(max_age=86400):
    """The human's GitHub login: config, else `gh api user` (cached a day), else None."""
    if login := get("user", "github"):
        return login
    path = os.path.join(STATE_DIR, "gh-login")
    try:
        if time.time() - os.path.getmtime(path) < max_age:
            return Path(path).read_text().strip() or None
    except OSError:
        pass
    try:
        login = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True,
                               timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(path, "w") as f:
            f.write(login)
        return login
    return None


def orgs():
    return [o for o in get("github", "orgs", []) if isinstance(o, str) and o]


def agent_kinds():
    """{name: definition} for every coding agent with a command, built-in or configured."""
    return {k: v for k, v in load().get("kinds", {}).items() if isinstance(v, dict) and v.get("cmd")}


def human_words():
    """Extra veto words as regex alternatives. Plain words are escaped; `\\w*` suffixes are kept."""
    out = []
    for w in get("watch", "human_words", []):
        if isinstance(w, str) and w.strip():
            out.append(re.escape(w.strip()).replace(re.escape(r"\w*"), r"\w*"))
    return out


def ship_policy(repo, owner=None, base=None):
    """ship's policy for one repo: [ship] + [repos.<repo>] + the repo's own .fleet.toml on its
    base branch (tighten-only). human_paths = protect.paths + the repo's protect_paths."""
    tree = load()
    pol = copy.deepcopy(tree["ship"])
    pol.setdefault("tier", None)
    over = copy.deepcopy(tree.get("repos", {}).get(repo, {}))
    for k, v in over.pop("tiers", {}).items():
        pol["tiers"].setdefault(k, {}).update(v)
    extra = over.pop("protect_paths", [])
    pol.update(over)
    pol["human_paths"] = list(tree["protect"]["paths"]) + [p for p in extra if p not in tree["protect"]["paths"]]
    pol["repo_file_ignored"] = []
    if owner and base:
        pol["repo_file_ignored"] = tighten(pol, repo_file(owner, repo, base))
    return pol


# ── schema: what doctor and setup check a file against ───────────────────────

ENUMS = {"ship.merge": ("ask", "auto"), "ship.method": ("squash", "merge", "rebase"),
         "ship.deploy": ("actions", "none"), "ship.checks": ("required", "all"),
         "agents.permissions": ("ask", "bypass"), "decisions.default": ("off", "shadow", "act"),
         "new.classifier": ("auto", "jev", "decisions", "haiku")}
# Tables whose keys are names the user picks; the value checks below still apply inside them.
NAMED = {"kinds", "roles", "models", "hints", "repos", "dev.repos", "decisions", "ship.tiers", "remote.scopes"}
NAMED_FIELDS = {"kinds": ("cmd", "bypass", "model", "model_flag", "system_flag", "about", "names", "models",
                          "models_cmd"),
                "roles": ("kind", "model", "effort"), "models": ("harness", "model", "names"),
                "dev.repos": ("path", "base"), "ship.tiers": tuple(DEFAULTS["ship"]["tiers"]["normal"])}
REPO_ONLY = ("protect_paths", "tier")  # [repos.<name>] takes these besides any [ship] key


def validate(data):
    """Problems in one config file's raw TOML, as [{"key", "level": "warn"|"fail", "msg"}].
    Unknown keys warn with the closest known one (a typo silently does nothing); an invalid
    choice fails (`ship.merge = "yolo"` would otherwise read as the default)."""
    import difflib
    out = []

    def bad(level, key, msg):
        out.append({"key": key, "level": level, "msg": msg})

    def unknown(key, name, known):
        close = difflib.get_close_matches(name, known, n=1)
        bad("warn", key, "unknown setting" + (f"; did you mean {close[0]}?" if close else ""))

    def check_value(key, v, default):
        if key in ENUMS and v not in ENUMS[key]:
            bad("fail", key, f"{v!r} isn't one of {', '.join(ENUMS[key])}")
        elif default is not None and type(v) is not type(default) and not (
                isinstance(default, float) and isinstance(v, int)):
            bad("fail", key, f"expected {type(default).__name__}, got {type(v).__name__}")  # tools would crash

    def walk(tree, schema, prefix):
        for k, v in tree.items():
            key = prefix + k
            if prefix.rstrip(".") in NAMED:
                walk_named(prefix.rstrip("."), k, v)
            elif k not in schema:
                unknown(key, k, list(schema))
            elif isinstance(schema[k], dict) and key not in ("hints",) and (schema[k] or key in NAMED):
                if not isinstance(v, dict):
                    bad("warn", key, "expected a table")
                else:
                    walk(v, schema[k], key + ".")
            else:
                check_value(key, v, schema[k])

    def walk_named(section, name, v):
        key = f"{section}.{name}"
        if section == "hints":
            return check_value(key, v, "")
        if section == "remote.scopes":
            if not name.startswith("tag:"):
                bad("fail", key, "scopes name Tailscale tags (tag:<name>); people and untagged nodes get nothing")
            elif not (isinstance(v, list) and all(isinstance(t, str) for t in v)):
                bad("warn", key, "expected a list of tool names")
            return
        if section == "decisions":
            if name == "model":
                return check_value(key, v, "")
            if v not in ENUMS["decisions.default"]:
                bad("fail", key, f"{v!r} isn't one of {', '.join(ENUMS['decisions.default'])}")
            return
        if not isinstance(v, dict):
            return bad("warn", key, "expected a table")
        if section == "repos":
            allowed = list(DEFAULTS["ship"]) + list(REPO_ONLY)
            for f, x in v.items():
                if f not in allowed:
                    unknown(f"{key}.{f}", f, allowed)
                elif f == "tiers" and isinstance(x, dict):
                    for t, tv in x.items():
                        walk_named("ship.tiers", t, tv)
                elif f in DEFAULTS["ship"]:
                    check_value(f"ship.{f}", x, DEFAULTS["ship"][f])
            return
        for f in v:
            if f not in NAMED_FIELDS[section]:
                unknown(f"{key}.{f}", f, list(NAMED_FIELDS[section]))
        if section == "roles" and "kind" in v and v["kind"] not in (*agent_kinds(), "opposite"):
            bad("fail", f"{key}.kind", f"{v['kind']!r} isn't a known agent ({', '.join((*agent_kinds(), 'opposite'))})")
        if section == "models" and v.get("harness") not in agent_kinds():
            bad("fail", f"{key}.harness", f"{v.get('harness')!r} isn't a known agent ({', '.join(agent_kinds())})")

    walk({k: v for k, v in data.items() if k not in ("preset", "lock")}, DEFAULTS, "")
    return out


def check_files():
    """validate() for every file the config reads (personal, then the preset), keys prefixed
    with nothing but each problem naming its file."""
    files = [PATH]
    personal = _toml(PATH)
    if personal.get("preset"):
        files.append(os.path.expanduser(personal["preset"]))
    out = []
    for f in files:
        for p in validate(_toml(f, required=f != PATH)):
            out.append(dict(p, file=f))
    return out


# ── repo layer: tighten-only ─────────────────────────────────────────────────

def repo_file(owner, repo, base, max_age=600):
    """<repo>/.fleet.toml on the base branch, from the GitHub API (never the PR head: an agent can
    edit the file on its branch). Cached 10 min; missing file → {}."""
    safe = re.sub(r"[^\w.-]", "_", f"{owner}__{repo}__{base}")
    path = os.path.join(STATE_DIR, "repo-config", safe + ".toml")
    try:
        fresh = time.time() - os.path.getmtime(path) < max_age
    except OSError:
        fresh = False
    if not fresh:
        try:
            p = subprocess.run(["gh", "api", f"repos/{owner}/{repo}/contents/.fleet.toml?ref={base}",
                                "-H", "Accept: application/vnd.github.raw"], capture_output=True, text=True,
                               timeout=60)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"can't read {owner}/{repo}/.fleet.toml@{base}: gh timed out")
        if p.returncode != 0 and "404" not in p.stderr:
            raise RuntimeError(f"can't read {owner}/{repo}/.fleet.toml@{base}: {p.stderr.strip()[:200]}")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # atomic: a reader mid-write would parse an empty file and drop the repo's tighten-only rules
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w") as f:
            f.write(p.stdout if p.returncode == 0 else "")
        os.replace(tmp, path)
    try:
        return tomllib.loads(Path(path).read_text())
    except tomllib.TOMLDecodeError as e:
        raise RuntimeError(f"{owner}/{repo}/.fleet.toml@{base}: {e}")


MERGE_RANK = {"auto": 0, "ask": 1}


def tighten(pol, data):
    """Apply a repo file to a ship policy, keeping only changes that make ship more careful.
    Returns [(key, why)] for everything ignored."""
    ignored = []
    for section, values in data.items():
        if not isinstance(values, dict):
            ignored.append((section, "not a table"))
            continue
        for k, v in values.items():
            key = f"{section}.{k}"
            if key == "protect.paths" and isinstance(v, list) and all(isinstance(x, str) for x in v):
                pol["human_paths"] += [x for x in v if x not in pol["human_paths"]]
            elif key == "ship.tier" and v == "critical":
                pol["tier"] = "critical"
            elif key == "ship.merge" and v in MERGE_RANK and MERGE_RANK[v] >= MERGE_RANK.get(pol["merge"], 1):
                pol["merge"] = v
            elif key in ("ship.require_selfreview", "ship.cross_review") and v is True:
                pol[k] = True
            elif key == "ship.deploy" and pol.get("deploy") == "none" and isinstance(v, str) and v != "none":
                pol["deploy"] = v  # watching deploys is tighter; ship rejects unknown names
            elif key == "ship.finding_bots" and isinstance(v, list) and all(isinstance(x, str) and x for x in v):
                pol["finding_bots"] = list(pol.get("finding_bots") or []) + [
                    x for x in v if x not in (pol.get("finding_bots") or [])]  # more blockers: tighter
            elif key == "ship.checks" and v == "all":
                pol["checks"] = "all"
            elif key == "ship.review" and pol.get("review") == "none" and isinstance(v, str) and v != "none":
                pol["review"] = v  # turning review on is tighter; ship rejects unknown names
            else:
                ignored.append((key, "repo files can only tighten (add protected paths, tier = \"critical\", "
                                     "merge = \"ask\", turn checks on)"))
    return ignored


# ── CLI ──────────────────────────────────────────────────────────────────────
def _key(k):
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def _val(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_key(k)} = {_val(x)}" for k, x in v.items()) + "}"
    raise TypeError(f"can't write {type(v).__name__} to TOML")


def _flat(tree, prefix=""):
    for k, v in tree.items():
        key = prefix + _key(k)
        if isinstance(v, dict) and v:
            yield from _flat(v, key + ".")
        else:
            yield key, v


def cmd_show(args):
    as_json = "--json" in args
    repo = args[args.index("--repo") + 1] if "--repo" in args else None
    args = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] != "--repo")]
    want = args[0] if args else ""
    r = resolve()
    rows = [(k, v, r.sources.get(k, "default")) for k, v in _flat(r.tree)
            if not want or k == want or k.startswith(want + ".")]
    ignored = list(r.ignored)
    pol = None
    if repo:
        owner, _, name = repo.partition("/")
        name, _, base = name.partition("@")
        if not (owner and name):
            sys.exit("fleet config show --repo owner/name[@base]")
        if not base:
            base = subprocess.run(["gh", "api", f"repos/{owner}/{name}", "--jq", ".default_branch"],
                                  capture_output=True, text=True).stdout.strip() or "main"
        pol = ship_policy(name, owner, base)
        ignored += [(k, f"repo {owner}/{name}@{base}", why) for k, why in pol.pop("repo_file_ignored")]
    if as_json:
        print(json.dumps({"files": {"personal": PATH}, "layers": r.layers,
                          "values": {k: {"value": v, "layer": s} for k, v, s in rows},
                          "ship_policy": pol, "ignored": [{"key": k, "layer": l, "why": w} for k, l, w in ignored]},
                         indent=1))
        return
    width = max((len(k) for k, _, _ in rows), default=0)
    for k, v, s in rows:
        print(f"{k.ljust(width)} = {_val(v)}   # {s}")
    if pol:
        print(f"\nship policy for {repo}:\n" + json.dumps(pol, indent=1))
    for k, layer, why in ignored:
        print(f"ignored: {k} from {layer}: {why}")


def cmd_get(key):
    """One value, bare, for shell tools: a string as is, anything else as JSON."""
    cur = load()
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            sys.exit(f"fleet config get: no setting {key}")
        cur = cur[part]
    print(cur if isinstance(cur, str) else json.dumps(cur))


def main(argv):
    if argv[:1] == ["show"]:
        cmd_show(argv[1:])
    elif argv[:1] == ["get"] and len(argv) == 2:
        cmd_get(argv[1])
    else:
        sys.exit("usage: fleet config show [key] [--repo owner/name[@base]] [--json] | get <section.key>")


if __name__ == "__main__":
    try:
        main(sys.argv[1:] or ["show"])
    except RuntimeError as e:
        sys.exit(f"fleet config: {e}")
