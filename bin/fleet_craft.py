"""fleet_craft — the craft review's findings and the verdicts agents give them.

One file per PR, ~/.local/state/fleet/reviews/<owner>__<repo>__<n>.json, never a PR comment:
  {"reviews": [{"head", "kind", "at", "failed"?, "findings": [
      {"id", "lens", "severity", "file", "line", "summary", "detail", "generalises",
       "verdict"?, "why"?, "decided_at"?, "decided_head"?}]}]}
The last review that ran gates the PR. Verdicts are what `fleet learn` compounds: a finding
agents keep fixing is a rule or lint waiting to be written; one they keep dismissing is noise
the brief should stop raising. prune() drops the long text after a while and keeps those.
"""
import json, os, time
from datetime import datetime, timezone

from fleet_state import read, update

LENSES = ("arch", "craft", "test", "clarity", "simplify")
SEVERITIES = ("concern", "suggestion", "nit")
VERDICTS = ("fixed", "dismissed")
MAX_FINDINGS = 15  # the brief asks for at most this many, strongest first; more is padding


def path(state_dir, repo_full, n):
    return os.path.join(state_dir, "reviews", f"{repo_full.replace('/', '__')}__{n}.json")


def load(state_dir, repo_full, n):
    return read(path(state_dir, repo_full, n), {"reviews": []})


def each(state_dir):
    """(repo_full, n, data) for every PR with a craft review. A corrupt file raises (fleet_state)."""
    root = os.path.join(state_dir, "reviews")
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else ():
        if name.endswith(".json"):
            owner_repo, n = name[:-5].rsplit("__", 1)
            yield owner_repo.replace("__", "/", 1), int(n), read(os.path.join(root, name), {"reviews": []})


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse(text):
    """Findings from the reviewer's JSON lines; anything else it said is ignored. None when it
    never said it was done (crashed, cut off): no findings is then not the same as clean."""
    lines = [x.strip() for x in text.splitlines()]
    if "END" not in lines:
        return None
    out = []
    for line in lines:
        try:
            d = json.loads(line) if line.startswith("{") else None
        except json.JSONDecodeError:
            d = None
        if not isinstance(d, dict) or not d.get("summary"):
            continue
        lens, sev = str(d.get("lens", "")).lower(), str(d.get("severity", "")).lower()
        out.append({"id": f"c{len(out) + 1}",
                    "lens": lens if lens in LENSES else "craft",
                    "severity": sev if sev in SEVERITIES else "suggestion",
                    "file": str(d.get("file") or "")[:200],
                    "line": d["line"] if isinstance(d.get("line"), int) else None,
                    "summary": str(d["summary"])[:300],
                    "detail": str(d.get("detail") or "")[:2000],
                    "generalises": str(d.get("generalises") or "")[:300]})
        if len(out) == MAX_FINDINGS:
            break
    return out


def add(state_dir, repo_full, n, head, kind, findings, failed=None):
    rec = {"head": head, "kind": kind, "at": now_iso(), "findings": findings or []}
    if failed:
        rec["failed"] = failed[:300]
    os.makedirs(os.path.join(state_dir, "reviews"), mode=0o700, exist_ok=True)
    update(path(state_dir, repo_full, n), lambda d: d["reviews"].append(rec), {"reviews": []})
    return rec


def latest(data):
    """(the last review that ran, how many runs failed after it)."""
    done = [i for i, r in enumerate(data["reviews"]) if not r.get("failed")]
    after = data["reviews"][done[-1] + 1:] if done else data["reviews"]
    return (data["reviews"][done[-1]] if done else None), len(after)


def undecided(review):
    return [f for f in review["findings"] if not f.get("verdict")]


def decide(state_dir, repo_full, n, ids, verdict, why, head=None):
    """Record a verdict on findings of the latest review. Returns the ids it didn't know."""
    unknown = list(ids)

    def apply(data):
        last, _ = latest(data)
        for f in (last or {"findings": []})["findings"]:
            if f["id"] in ids:
                f.update(verdict=verdict, why=why[:500], decided_at=now_iso(), decided_head=head)
                unknown.remove(f["id"])
    update(path(state_dir, repo_full, n), apply, {"reviews": []})
    return unknown


def _drop_detail(data):
    for r in data["reviews"]:
        for f in r["findings"]:
            f.pop("detail", None)


def prune(state_dir, keep_days, now=None):
    """Drop the long text (detail) of reviews untouched for keep_days; keep what learning needs."""
    root = os.path.join(state_dir, "reviews")
    cutoff = (now or time.time()) - keep_days * 86400
    pruned = 0
    for name in os.listdir(root) if os.path.isdir(root) else ():
        p = os.path.join(root, name)
        if not name.endswith(".json") or os.path.getmtime(p) > cutoff:
            continue
        update(p, _drop_detail, {"reviews": []})
        pruned += 1
    return pruned
