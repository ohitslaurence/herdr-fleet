"""fleet_pr — one fleet PR in full: its diff, its checks and what reviewers said.

For a reader who isn't on this machine (the remote API's fleet_pr). `fleet watch` runs it, only for
PRs on the cockpit's live rows (not the backlog), so a caller can't point gh at anything else.
GitHub calls run on this machine's gh login, so they're bounded: one fetch at a time per PR, answers
(failures too) cached per head commit, and at most FETCHES_PER_HOUR fetches. All text is redacted
before it's capped (agents, PR authors and reviewers wrote it).
"""
import sys, threading, time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import fleet_proc
from fleet_redact import redact

DIFF_CHARS = 24000   # a reader's context, not the whole PR: `diff_chars` says how much there was
BODY_CHARS = 2000
COMMENT_CHARS = 800
MAX_COMMENTS = 40    # the newest, across reviews, conversation and inline comments
MAX_FILES = 100
GH_TIMEOUT_S = 8     # under the remote API's deadline (a test checks); the three calls run in parallel
CACHE_S = 300
FAILED_CACHE_S = 60  # a PR GitHub won't serve isn't asked for again by every caller
CACHE_SIZE = 64      # more than the live rows a fleet has: a caller walking them all still hits it
FETCHES_PER_HOUR = 60  # 3 gh calls each: a few percent of GitHub's 5,000 an hour, which agents share

_cache, _gates, _fetches, _lock = {}, {}, deque(), threading.Lock()


class Refused(Exception):
    """Why fleet_pr won't answer, as a code the caller can act on: not_found and ambiguous (the
    caller's ref), budget (try later), upstream (GitHub failed). The watcher passes it on as is."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def find(rows, ref):
    """The live PR row whose `where` (api#12), owner/repo#12 or url is ref; Refused if none or several."""
    hits = {(r["repo"], r["number"]): r for r in rows
            if r.get("kind") == "pr" and r.get("section") != "BACKLOG" and r.get("repo") and r.get("number")
            and ref in (r.get("where"), r.get("url"), f"{r['repo']}#{r['number']}")}
    if len(hits) > 1:
        names = ", ".join(sorted(f"{repo}#{n}" for repo, n in hits))
        raise Refused("ambiguous", f"{ref} matches {len(hits)} PRs in different repos ({names}); use one of those")
    if not hits:
        raise Refused("not_found", f"{ref} isn't a PR in the fleet (see fleet_overview)")
    return next(iter(hits.values()))


def gh_json(*args):
    return fleet_proc.gh_json(*args, timeout=GH_TIMEOUT_S)


def gh_text(*args):
    return fleet_proc.gh_text(*args, timeout=GH_TIMEOUT_S)


def check(c):
    if c.get("__typename") == "StatusContext":
        return {"name": c.get("context"), "state": c.get("state"), "url": c.get("targetUrl")}
    return {"name": c.get("name"), "state": c.get("conclusion") or c.get("status"), "url": c.get("detailsUrl")}


def comment(kind, who, at, body, **where):
    return {"kind": kind, "author": (who or {}).get("login"), "at": at, **where,
            "body": (body or "")[:COMMENT_CHARS]}


def fetch(repo, number):
    n = str(number)
    with ThreadPoolExecutor(3) as pool:
        view = pool.submit(gh_json, "pr", "view", n, "-R", repo, "--json",
                           "title,body,state,isDraft,headRefOid,baseRefName,headRefName,files,reviews,comments,"
                           "statusCheckRollup")
        diff = pool.submit(gh_text, "pr", "diff", n, "-R", repo)
        inline = pool.submit(gh_json, "api", f"repos/{repo}/pulls/{n}/comments?per_page=100")
        view, diff, inline = view.result(), diff.result(), inline.result()
    view, diff, inline = redact(view), redact(diff), redact(inline)  # before any cap cuts a token in half
    if view is None:
        raise RuntimeError(f"gh pr view {repo}#{n} failed")
    talk = [comment("review", r.get("author"), r.get("submittedAt"), r.get("body"), state=r.get("state"))
            for r in view.get("reviews") or [] if r.get("body") or r.get("state") != "COMMENTED"]
    talk += [comment("comment", c.get("author"), c.get("createdAt"), c.get("body"))
             for c in view.get("comments") or []]
    talk += [comment("inline", c.get("user"), c.get("created_at"), c.get("body"), path=c.get("path"),
                     line=c.get("line")) for c in (inline if isinstance(inline, list) else [])]
    talk.sort(key=lambda c: c["at"] or "")
    files = view.get("files") or []
    return {
        "repo": repo, "number": number, "title": view.get("title"), "state": view.get("state"),
        "draft": view.get("isDraft"), "head": view.get("headRefOid"), "branch": view.get("headRefName"),
        "base": view.get("baseRefName"), "body": (view.get("body") or "")[:BODY_CHARS],
        "files": [{"path": f.get("path"), "additions": f.get("additions"), "deletions": f.get("deletions")}
                  for f in files[:MAX_FILES]],
        "files_count": len(files),
        "checks": [check(c) for c in view.get("statusCheckRollup") or []],
        "comments": talk[-MAX_COMMENTS:], "comments_count": len(talk),
        "diff": None if diff is None else diff[:DIFF_CHARS],
        "diff_chars": None if diff is None else len(diff),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _cached(key, now):
    """The cached answer, None if there's none; a cached failure is raised again."""
    hit = _cache.get(key)
    if not hit or now - hit[0] >= hit[1]:
        return None
    if isinstance(hit[2], Refused):
        raise hit[2]
    return hit[2]


def _budget_left(now):
    while _fetches and now - _fetches[0] > 3600:
        _fetches.popleft()
    return len(_fetches) < FETCHES_PER_HOUR


def detail(rows, ref, now=None):
    """The PR named by ref (Refused if not, with why). Cached per (PR, head commit), failures too."""
    row = find(rows, ref)
    now = time.time() if now is None else now
    key = (row["repo"], row["number"], row.get("head"))
    with _lock:
        hit = _cached(key, now)
        if hit is not None:
            return hit
        gate = _gates.setdefault(key, threading.Lock())
    with gate:  # one fetch per PR at a time: concurrent callers wait for it and share its answer
        try:
            return _fetch_once(key, row, ref, now)
        finally:
            with _lock:
                _gates.pop(key, None)  # whatever happened (answer, refusal, crash), no gate is left behind


def _fetch_once(key, row, ref, now):
    """Under the PR's gate: the cached answer if another caller just fetched it, else one fetch."""
    with _lock:
        hit = _cached(key, now)
        if hit is not None:
            return hit
        if not _budget_left(now):
            raise Refused("budget", f"fleet_pr's GitHub budget ({FETCHES_PER_HOUR} fetches an hour) is "
                                    "spent; cached PRs still answer")
        _fetches.append(now)
    got, ttl = Refused("upstream", f"GitHub didn't answer for {ref}; try again in a minute"), FAILED_CACHE_S
    try:
        got, ttl = fetch(row["repo"], row["number"]), CACHE_S
    except Exception as e:  # gh failed or answered something unexpected: cached briefly, like a failure
        print(f"fleet_pr: {row['repo']}#{row['number']}: {type(e).__name__}: {e}"[:300], file=sys.stderr)
    with _lock:
        _cache[key] = (now, ttl, got)
        for old in sorted(_cache, key=lambda k: _cache[k][0])[:-CACHE_SIZE]:
            del _cache[old]
    if isinstance(got, Refused):
        raise got
    return got
