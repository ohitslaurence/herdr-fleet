"""fleet_gh — many PR lookups in one GitHub GraphQL call.

fleet watch asked `gh pr list` once per pane every pass, and the cockpit refresh ran `ship` once
per PR every 10 minutes: ~70 processes a pass, most of the watcher's CPU. These batch the
lookups (aliases, CHUNK per request) so a pass costs one or two `gh` processes.

Missing from a result means unknown (the request failed): callers must not read that as
"no PR". A PR that doesn't exist maps to None.

  prs_for_branches([("owner/repo", "branch"), ...]) -> {(repo, branch): pr | None}
      pr = {number, state, url, mergedAt, closedAt, title} (newest PR for that head branch)
  pr_signatures(["https://github.com/o/r/pull/1", ...]) -> {url: "sig" | None}
      sig changes whenever anything ship looks at may have changed: head, updatedAt, CI
      rollup, mergeability against the base, review decision, draft.
"""
import json, re, subprocess

CHUNK = 40
PR = "number state url mergedAt closedAt title"
SIG = ("state headRefOid updatedAt isDraft mergeable mergeStateStatus reviewDecision "
       "commits(last:1){nodes{commit{statusCheckRollup{state}}}}")
URL = re.compile(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def _query(parts, timeout=60):
    """Run one aliased query; {alias: value} for the aliases GitHub answered. A partial error
    (e.g. a deleted repo) still returns the other aliases."""
    q = "query{" + " ".join(parts) + "}"
    try:
        p = subprocess.run(["gh", "api", "graphql", "-f", f"query={q}"], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    try:
        data = json.loads(p.stdout or "{}").get("data") or {}
    except ValueError:
        return {}
    return data


def _chunks(items):
    for i in range(0, len(items), CHUNK):
        yield items[i:i + CHUNK]


def prs_for_branches(pairs):
    pairs = sorted(set(pairs))
    out = {}
    for chunk in _chunks(pairs):
        parts = []
        for i, (repo, branch) in enumerate(chunk):
            owner, _, name = repo.partition("/")
            parts.append(f'b{i}:repository(owner:{json.dumps(owner)},name:{json.dumps(name)}){{'
                         f'pullRequests(headRefName:{json.dumps(branch)},first:1,'
                         f'orderBy:{{field:CREATED_AT,direction:DESC}}){{nodes{{{PR}}}}}}}')
        data = _query(parts)
        for i, pair in enumerate(chunk):
            if f"b{i}" not in data:
                continue  # unknown, not "no PR"
            repo = data[f"b{i}"]
            nodes = ((repo or {}).get("pullRequests") or {}).get("nodes") or []
            out[pair] = nodes[0] if nodes else None
    return out


def pr_signatures(urls):
    refs = sorted({u for u in urls if URL.match(u)})
    out = {}
    for chunk in _chunks(refs):
        parts = []
        for i, u in enumerate(chunk):
            o, r, n = URL.match(u).groups()
            parts.append(f'p{i}:repository(owner:{json.dumps(o)},name:{json.dumps(r)}){{pullRequest(number:{n}){{{SIG}}}}}')
        data = _query(parts)
        for i, u in enumerate(chunk):
            if f"p{i}" not in data:
                continue
            pr = (data[f"p{i}"] or {}).get("pullRequest")
            out[u] = json.dumps(pr, sort_keys=True) if pr else None
    return out
