"""Record golden fixtures for ship's decide() from real PRs, anonymised.

  python3 tests/golden/record.py <pr-url> [<pr-url> ...]

For each PR: gather LiveFacts (network), anonymise everything that could identify the org or
its code, run decide() on both the real and the anonymised facts, and keep the fixture only if
they reach the same state. The fixture's expected verdict is decide() on the anonymised facts,
so the golden test compares like with like.

What survives anonymisation is only what ship reads: structure, states, timestamps, SHAs,
counts, and the markers ship parses out of comment bodies (scores, P0-P2 tags, review requests,
fleet markers). Titles, branch names, bodies, people, check/workflow names, URLs and file paths
are replaced.
"""
import copy, hashlib, importlib.machinery, importlib.util, json, os, re, sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "..", "..", "bin")
sys.path.insert(0, BIN)
sys.path.insert(0, HERE)
from facts import RecordedFacts, load_ship  # noqa: E402

# (pattern, rebuild): every marker is rebuilt from its parsed value only, so no surrounding text
# (commit messages, a human's words after a mention) survives.
KEEP = [
    (r"greptile_confidence_score:(\d)", lambda m: f"greptile_confidence_score:{m.group(1)}"),
    (r"Confidence Score:\s*(\d)/5", lambda m: f"Confidence Score: {m.group(1)}/5"),
    (r"Last reviewed commit:.*?/commit/([0-9a-f]{40})",
     lambda m: f"Last reviewed commit: https://github.com/acme/app/commit/{m.group(1)}"),
    (r"\[Critical (impact|risk)\]", lambda m: f"[Critical {m.group(1)}]"),
    (r"@greptileai", lambda m: "@greptileai please re-review the latest commit"),
    (r'alt="(P[0-2])"', lambda m: f'alt="{m.group(1)}"'),
    (r"<!-- fleet-(self-review|rescue) sha=([0-9a-f]{7,40}) -->", lambda m: f"<!-- fleet-{m.group(1)} sha={m.group(2)} -->"),
    (r"<!-- fleet-review sha=([0-9a-f]{7,40}) verdict=(clean|findings) -->",
     lambda m: f"<!-- fleet-review sha={m.group(1)} verdict={m.group(2)} -->"),
    (r"^\*\*Cross-family review\*\*", lambda m: "**Cross-family review**"),
]


class Anon:
    def __init__(self, bots):
        self.maps, self.bots = {}, bots

    def name(self, kind, value):
        if value is None:
            return None
        m = self.maps.setdefault(kind, {})
        if value not in m:
            m[value] = f"{kind}-{len(m) + 1}"
        return m[value]

    def login(self, author):
        if not author:
            return author
        login = author.get("login") or ""
        if any(b in login for b in self.bots):
            return author
        return {"login": self.name("user", login)}

    def body(self, text):
        if not text:
            return text
        kept = [rebuild(m) for rx, rebuild in KEEP for m in re.finditer(rx, text, re.M)]
        return "\n".join(kept) or "(text)"

    def url(self, u):
        return self.name("url", u) and f"https://example.invalid/{self.name('url', u)}"


def anonymise(pr, prev, calls, protected, bots):
    """-> (pr, prev, calls, human_paths) with nothing identifying left."""
    a = Anon(bots)
    pr = copy.deepcopy(pr)
    n = pr["number"]
    pr.update(url=f"https://github.com/acme/app/pull/{n}", title=f"PR {n}", headRefName=f"branch-{n}")
    if pr["baseRefName"] not in ("main", "master"):
        pr["baseRefName"] = "base-branch"

    def ids(x):  # GraphQL node ids encode internal repo ids
        if isinstance(x, dict):
            for k, v in x.items():
                if k == "id" and isinstance(v, str):
                    x[k] = a.name("id", v)
                else:
                    ids(v)
        elif isinstance(x, list):
            for v in x:
                ids(v)
    ids(pr)
    pr["labels"]["nodes"] = [x for x in pr["labels"]["nodes"] if x["name"].startswith("agent:")]
    files = []
    for i, f in enumerate(pr["files"]["nodes"]):
        files.append(dict(f, path=(f"protected/f{i}" if f["path"] in protected else f"src/f{i}")))
    pr["files"]["nodes"] = files
    for c in (pr["commits"]["nodes"] or []):
        for x in (((c["commit"].get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or []):
            for k in ("name", "context"):
                if x.get(k):
                    x[k] = a.name("check", x[k])
            for k in ("detailsUrl", "targetUrl"):
                if x.get(k):
                    x[k] = a.url(x[k])
            wf = ((x.get("checkSuite") or {}).get("workflowRun") or {}).get("workflow")
            if wf:
                wf["name"] = a.name("workflow", wf["name"])
    for x in pr["reviews"]["nodes"]:
        x["author"], x["body"] = a.login(x["author"]), a.body(x["body"])
    for x in pr["latestOpinionatedReviews"]["nodes"]:
        x["author"] = a.login(x["author"])
    for x in pr["comments"]["nodes"]:
        x["author"], x["body"] = a.login(x["author"]), a.body(x["body"])
        if x.get("url"):
            x["url"] = a.url(x["url"])
    for t in pr["reviewThreads"]["nodes"]:
        for c in t["comments"]["nodes"]:
            c["author"], c["body"] = a.login(c.get("author")), a.body(c.get("body"))
        for c in ((t.get("last") or {}).get("nodes") or []):
            c["author"] = a.login(c.get("author"))
    calls = copy.deepcopy(calls)
    calls["files"] = [f["path"] for f in files]
    if calls.get("required"):
        calls["required"] = sorted(a.name("check", x) for x in calls["required"])
    if "posted" in calls:
        calls["posted"] = sorted(a.url(u) for u in calls["posted"])
    for k, v in list(calls.items()):
        if k.startswith("deploy:") and v and v[0] != "error":
            calls[k] = [v[0], re.sub(r"\S+", lambda m: a.name("word", m.group(0)), v[1] or "")]
    keep = ("head", "head_seen_at", "review")
    prev = {k: v for k, v in (prev or {}).items() if k in keep}
    return pr, prev, calls, ["protected/*"]


def main(urls):
    # the real ledger and caches: previous records (head_seen_at, review rounds) are facts too
    S = load_ship()
    S.STATE_DIR = os.path.expanduser("~/.local/state/fleet")
    out_dir = os.path.join(HERE, "fixtures")
    os.makedirs(out_dir, exist_ok=True)
    for u in urls:
        o, r, _, n = u.rstrip("/").split("/")[3:7]
        live = S.LiveFacts(o, r, int(n))
        bots = S.finding_bots(live.pol)  # configured bots keep their logins, so their threads still block
        rec = RecordedFacts.capture(live)
        now = datetime.now(timezone.utc)
        real, _, _ = S.decide(rec, now)
        protected = set(S.human_files(rec.calls["files"], live.pol["human_paths"]))
        pr, prev, calls, human_paths = anonymise(live.pr, rec.calls.get("prev"), rec.calls, protected, bots)
        pol = dict(live.pol, human_paths=human_paths, repo_file_ignored=[])
        calls["prev"] = prev
        fx = {"now": now.isoformat(), "pr": pr, "policy": pol, "calls": calls}
        anon_s, _, _ = S.decide(RecordedFacts(fx), now)
        if anon_s["state"] != real["state"]:
            print(f"skip {u}: anonymised state {anon_s['state']} != real {real['state']}", file=sys.stderr)
            continue
        fx["expected"] = {k: anon_s.get(k) for k in EXPECT}
        name = f"{real['state']}-{hashlib.sha1(u.encode()).hexdigest()[:8]}.json"
        with open(os.path.join(out_dir, name), "w") as f:
            json.dump(fx, f, indent=1, sort_keys=True)
        print(f"{name}  ({real['state']})")


EXPECT = ("state", "next", "act", "tier", "rereviews_left", "failing", "pending", "detail", "never_started",
          "optional_failing", "lines_since_review")

if __name__ == "__main__":
    main(sys.argv[1:])
