"""fleet_decide — the fleet's decision layer.

Every automated action states its *why* and, before acting, asks a fast typed judgment
(TypeSafe Jev) whether it still serves that why given what is actually happening. Jev only
judges (choice / noul / score with probabilities); code keeps every hard gate.

    from fleet_decide import decide, context
    ctx = context(pane="w1:p1", pr=("acme/app", 42))
    d = decide("watch.offer", action="auto-reply yes to the agent's offer",
               why="spare the user from approving routine local steps",
               subject="wE6:p1", state=ctx, questions={...}, rule="inbox")
    if d.act and d.answers["kind"]["choice"] == "local-reversible": ...

Rules (enforced by callers, logged here):
  1. Monotonic: answers may only make an action more careful. Never unlock a hard gate.
  2. Fail open to the existing rule: d.answers is None on timeout/error/off.
  3. Agent- or PR-written text is untrusted state: keep keyword vetoes in code.
  4. Shadow first: per point, [decisions] in the fleet config (fleet_config) says "shadow" (log only,
     default), "act" (caller may use answers) or "off" (no call).
  5. Every call is a line in ~/.local/state/fleet/decisions.jsonl; `fleet why` reads them.
"""
import hashlib, json, os, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet_config  # noqa: E402
import fleet_review  # noqa: E402
from fleet_keys import keys  # noqa: E402

STATE_DIR = os.path.expanduser("~/.local/state/fleet")
LOG = os.path.join(STATE_DIR, "decisions.jsonl")
CACHE = os.path.join(STATE_DIR, "cache")
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"


from fleet_redact import redact  # noqa: E402  (secrets never leave the machine: see there)


def mode(point):
    cfg = fleet_config.load()["decisions"]
    return cfg.get(point, cfg.get("default", "shadow"))


def debug():
    return bool(fleet_config.get("decisions", "debug"))


def jev_request(key, state, questions):
    model = fleet_config.get("decisions", "model", MODEL)
    body = json.dumps({"state": redact(state), "model": model, "questions": questions}).encode()
    return urllib.request.Request(ENDPOINT, data=body, headers={"Authorization": f"Bearer {key}",
                                                               "Content-Type": "application/json"})


def jev(state, questions, timeout=1.5):
    """One request; questions run in parallel. Raises on any failure: callers fall back.
    The shared request builder redacts state so no caller can forget to."""
    key = keys().get("TYPESAFE_API_KEY")
    if not key:
        raise RuntimeError("no TYPESAFE_API_KEY in ~/.config/fleet/env")
    req = jev_request(key, state, questions)
    for attempt in (1, 2):  # 429/529: one short backoff, then give up (the rule decides)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)["answers"]
        except urllib.error.HTTPError as e:
            if e.code not in (429, 529) or attempt == 2:
                raise
            time.sleep(0.2)


def compact(answers):
    """Just what a human needs to read back: the pick and how sure."""
    out = {}
    for k, a in (answers or {}).items():
        if a.get("type") == "choice":
            out[k] = {"choice": a["choice"], "p": round(a["probabilities"].get(a["choice"], 0), 2),
                      "conf": round(a.get("confidence", 0), 2)}
        elif a.get("type") == "noul":
            out[k] = {"yes": round(a["noul"], 2)}
        elif a.get("type") == "score":
            out[k] = {"score": round(a["score"], 2), "conf": round(a.get("confidence", 0), 2)}
        else:
            out[k] = a
    return out


class Decision:
    def __init__(self, point, mode, answers, error, ms):
        self.point, self.mode, self.answers, self.error, self.ms = point, mode, answers, error, ms

    @property
    def act(self):
        """True only when this point is in act mode AND Jev answered."""
        return self.mode == "act" and self.answers is not None

    def choice(self, q):
        a = (self.answers or {}).get(q) or {}
        return a.get("choice"), a.get("probabilities", {}).get(a.get("choice"), 0.0)

    def yes(self, q):
        return ((self.answers or {}).get(q) or {}).get("noul")


def decide(point, action, why, state, questions, subject="", rule=None, timeout=1.5, propose=None):
    """propose(answers) -> short string: what the caller would do on Jev's answers. Logged
    next to `rule` so shadow mode measures agreement before a point is allowed to act."""
    m = mode(point)
    answers = error = None
    t0 = time.monotonic()
    if m != "off":
        try:
            answers = jev(state, questions, timeout)
        except Exception as e:  # never let the decision layer break the caller
            error = f"{type(e).__name__}: {str(e)[:160]}"
    ms = int((time.monotonic() - t0) * 1000)
    proposal = None
    if propose and answers is not None:
        try:
            proposal = propose(answers)
        except Exception as e:
            error = f"propose: {type(e).__name__}: {e}"
    # The log keeps a hash of the state, not its text: agent output and PR comments stay out
    # of decisions.jsonl. `[decisions] debug = true` keeps a short redacted tail for tuning.
    text = state if isinstance(state, str) else json.dumps(state, sort_keys=True, default=str)
    preview = redact(text)[-400:] if debug() else None
    log({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "point": point, "mode": m,
         "subject": subject, "action": action, "why": why, "rule": rule, "proposal": proposal,
         "questions": {k: q.get("instructions") if isinstance(q.get("instructions"), str) else k
                       for k, q in questions.items()},
         "answers": compact(answers), "error": error, "ms": ms,
         "state_sha": hashlib.sha256(text.encode()).hexdigest()[:12],
         **({"state_preview": preview} if preview else {})})
    return Decision(point, m, answers, error, ms)


def log(entry):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# ── context collectors: cheap, best effort, never raise ──────────────────────

def _run(*args, timeout=10):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def pane_tail(pane, chars=1500):
    out = _run("herdr", "pane", "read", pane)
    try:
        out = json.loads(out)["result"].get("text", out)
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    lines = [l.rstrip() for l in str(out).splitlines() if l.strip()]
    return "\n".join(lines)[-chars:]


def note_for(cwd):
    try:
        notes = json.loads(Path(os.path.join(STATE_DIR, "notes.json")).read_text())
    except (OSError, ValueError):
        return ""
    root = max((p for p in notes if cwd == p or cwd.startswith(p + "/")), key=len, default=None)
    return (notes.get(root) or {}).get("text", "") if root else ""


def pr_state(repo_full, n):
    try:
        s = json.load(open(os.path.join(STATE_DIR, "ledger", f"{repo_full.replace('/', '__')}__{n}.json")))
    except (OSError, ValueError):
        return {}
    keep = ("title", "state", "next", "tier", "human_paths", "checked_at")
    return {k: s[k] for k in keep if s.get(k)}


def human_comments(repo_full, n, k=3, max_age=300):
    """The human's last few comments on the PR (cached 5 min): their intent beats any rule.
    Whose comments: [user] github in config.toml, else gh's login."""
    login = fleet_config.github_login()
    if not login:
        return []
    path = os.path.join(CACHE, f"comments-{repo_full.replace('/', '__')}-{n}.json")
    try:
        if time.time() - os.path.getmtime(path) < max_age:
            return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        pass
    out = _run("gh", "api", f"repos/{repo_full}/issues/{n}/comments", "--paginate", "--jq",
               f'[.[] | select(.user.login=="{login}") | {{at: .created_at, body: .body[:400]}}]')
    try:
        got = [c for c in json.loads(out or "[]")
               if not fleet_review.is_review_request(c["body"]) and not c["body"].lstrip().startswith("<!--")][-k:]
    except ValueError:
        got = []
    os.makedirs(CACHE, exist_ok=True)
    json.dump(got, open(path, "w"))
    return got


def context(pane=None, cwd=None, pr=None, extra=None):
    """Named fields so questions can point at them (`agent_last_output`, `task`, ...)."""
    ctx = {}
    if pane:
        ctx["agent_last_output"] = pane_tail(pane)
    if cwd:
        ctx["task"] = note_for(cwd)
    if pr:
        ctx["pr"] = pr_state(*pr)
        ctx["human_recent_comments"] = human_comments(*pr)
    ctx.update(extra or {})
    return {k: v for k, v in ctx.items() if v}


# ── fleet why ────────────────────────────────────────────────────────────────

def why(match="", limit=20):
    """Recent decisions whose subject/action/point mentions `match`, oldest first."""
    try:
        lines = Path(LOG).read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not match or match in f"{e.get('subject')} {e.get('point')} {e.get('action')}":
            out.append(e)
            if len(out) >= limit:
                break
    return list(reversed(out))


def stats(point):
    """Shadow-mode scorecard: how often Jev's proposal matches what the rule did."""
    rows = [e for e in why(point, limit=10 ** 6) if e.get("point") == point]
    if not rows:
        return f"{point}: no decisions logged"
    answered = [e for e in rows if e.get("proposal")]
    same = [e for e in answered if (e.get("rule") or "").split(":")[0].strip() == e["proposal"].split(":")[0].strip()]
    ms = sorted(e["ms"] for e in rows)
    lines = [f"{point}: {len(rows)} decisions, {len(rows) - len(answered)} unanswered (errors/off), "
             f"agree {len(same)}/{len(answered)}, median {ms[len(ms) // 2]} ms"]
    for e in answered:
        if e not in same:
            lines.append(f"  differs {e['ts'][5:16]} {e.get('subject')}: rule={e['rule']}  jev={e['proposal']}")
    return "\n".join(lines)


def format_entry(e):
    ans = ", ".join(f"{k}={'/'.join(f'{a}:{b}' for a, b in v.items())}" for k, v in (e.get("answers") or {}).items())
    head = f"{e['ts'][5:16].replace('T', ' ')}  {e['point']:<14} [{e['mode']}] {e.get('subject') or ''}"
    body = f"    would: {e['action']}\n    why:   {e['why']}"
    body += f"\n    rule:  {e['rule']}" if e.get("rule") is not None else ""
    body += f"\n    jev →  {e['proposal']}" if e.get("proposal") else ""
    body += f"\n    jev:   {ans or '-'}  ({e['ms']} ms)" + (f"\n    error: {e['error']}" if e.get("error") else "")
    return f"{head}\n{body}"
