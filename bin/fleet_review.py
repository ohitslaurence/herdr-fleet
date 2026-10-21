"""fleet_review — review providers: the bots ship waits on, asks for re-reviews, and reads
findings from. ship's state machine only sees the normalised ReviewState below; everything
provider-specific (logins, score markup, how to ask for a review, how to tell it's installed)
lives in a Provider.

Selected by `[ship] review = "<name>"` (or "none") in the fleet config; repos override with
`[repos.<name>] review = ...`. Built-ins are registered in PROVIDERS. Never loaded from the
repo under review.

ReviewState (Provider.state):
  present           the bot has reviewed this PR at all
  reviewed_sha      commit its latest verdict covers; reviewed_head: that's the PR head
  score             its latest score (None if it doesn't score); compared with tier min_score
  critical_impact   the bot itself rates the change critical (raises the PR's tier)
  findings_by_round inline findings per reviewed commit, oldest first (churn detection)
  rounds            review re-requests so far (each one may be paid)
  retag_pending     a review is running or was requested < timeout_min ago and not answered
  retag_unanswered  the last request got no verdict
  verdict_at        when the latest verdict was given
"""
import json, os, re, sys, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import fleet_proc as proc  # noqa: E402


def age_minutes(iso, now=None):
    now = now or datetime.now(timezone.utc)
    return (now - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 60


def is_bot(author, login):
    return bool(author) and author["login"] == login


class Provider:
    name = title = ""
    logins = ()                  # GraphQL logins of the bot, matched exactly
    request_text = ""            # PR comment that asks for a (re-)review
    first_review_grace_min = 5   # a new PR this young without a review: still coming on its own
    timeout_min = 45             # a request with no verdict after this long counts as dropped
    probe_prs = 30

    def is_bot(self, author):
        return any(is_bot(author, l) for l in self.logins)

    def reviewing(self, pending_checks):
        """A review check of this bot is running on the head."""
        return False

    def is_request(self, comment):
        return False

    def installed(self, o, r, pr, state_dir):
        """Has the bot reviewed or commented on this PR or one of the repo's recent PRs?
        Without this, ship waits out the first-review grace and then asks a bot that isn't
        there. Cached per repo: a yes for a day, a no for an hour. Can't tell: True (the
        unanswered-request gate fails closed)."""
        if any(self.is_bot(x["author"]) for x in pr["reviews"]["nodes"] + pr["comments"]["nodes"]):
            return True
        cache = os.path.join(state_dir, f"{self.name}-repos.json")
        try:
            with open(cache) as f:
                c = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            c = {}
        key = f"{o}/{r}"
        hit = c.get(key)
        if hit and time.time() - hit["at"] < (86400 if hit["ok"] else 3600):
            return hit["ok"]
        q = """query($o:String!,$r:String!,$k:Int!){repository(owner:$o,name:$r){pullRequests(last:$k){nodes{
     reviews(first:10){nodes{author{login}}} comments(first:10){nodes{author{login}}}}}}}"""
        p = proc.run(["gh", "api", "graphql", "-f", f"query={q}", "-F", f"o={o}", "-F", f"r={r}",
                      "-F", f"k={self.probe_prs}"])
        try:
            prs = json.loads(p.stdout)["data"]["repository"]["pullRequests"]["nodes"]
        except (ValueError, KeyError, TypeError):
            return True
        if p.returncode != 0:
            return True
        ok = any(self.is_bot(x["author"]) for p_ in prs for x in p_["reviews"]["nodes"] + p_["comments"]["nodes"])
        c[key] = {"at": time.time(), "ok": ok}
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        tmp = cache + ".tmp"
        with open(tmp, "w") as f:
            json.dump(c, f, indent=1)
        os.replace(tmp, cache)
        return ok

    def state(self, pr, reviewing=False, now=None, timeout_min=None):
        raise NotImplementedError

    def severity(self, body):
        """P0..P2 from a finding's first comment, or "other"."""
        m = re.search(r'alt="(P[0-2])"', body or "")
        return m.group(1) if m else "other"


class Greptile(Provider):
    name, title = "greptile", "Greptile"
    logins = ("greptile-apps",)
    request_text = "@greptileai please re-review the latest commit"

    def reviewing(self, pending_checks):
        return any("greptile" in p.lower() for p in pending_checks)

    def is_request(self, comment):
        return "@greptileai" in (comment["body"] or "") and not self.is_bot(comment["author"])

    def state(self, pr, reviewing=False, now=None, timeout_min=None):
        """Greptile reports in two places: formal PR reviews, and a summary comment it edits in
        place on re-reviews (score marker + "Last reviewed commit" link). Whichever is newer is
        the verdict; a re-review can update only the summary. Its answers to thread replies are
        formal reviews too, but they're not verdicts: skip them."""
        head = pr["headRefOid"]

        def is_reply(x):
            cs = x["comments"]["nodes"]
            return (cs and all(c["replyTo"] for c in cs)) or (not cs and not (x["body"] or "").strip())

        reviews = [x for x in pr["reviews"]["nodes"] if self.is_bot(x["author"]) and not is_reply(x)]
        last = reviews[-1] if reviews else None
        summary = next((c for c in reversed(pr["comments"]["nodes"])
                        if self.is_bot(c["author"])
                        and re.search(r"Confidence Score|greptile_confidence_score", c["body"] or "")), None)

        def score_in(body):
            m = re.search(r"greptile_confidence_score:(\d)|Confidence Score:\s*(\d)/5", body or "")
            return int(m.group(1) or m.group(2)) if m else None

        review_at = last["submittedAt"] if last else ""
        summary_at = (summary.get("updatedAt") or summary["createdAt"]) if summary else ""
        m = re.search(r"Last reviewed commit:.*?/commit/([0-9a-f]{40})", (summary or {}).get("body") or "")
        summary_sha = m.group(1) if m else None
        if summary and summary_at >= review_at:
            reviewed_sha, score, verdict_at = summary_sha, score_in(summary["body"]), summary_at
        else:
            reviewed_sha = last["commit"]["oid"] if last and last["commit"] else None
            score = next((sc for x in reversed(reviews) if (sc := score_in(x["body"])) is not None), None)
            if score is None and summary:
                score = score_in(summary["body"])
            verdict_at = review_at
        # The impact tag lives in the summary; fall back to the newest real review.
        impact_text = (summary or {}).get("body") or (last or {}).get("body") or ""
        requests = [c for c in pr["comments"]["nodes"] if self.is_request(c)]
        # One round per reviewed commit (inline findings on it), oldest first. Greptile edits its
        # summary seconds after each formal review, so the summary only adds a round when it
        # names a commit no formal review covers (a summary-only re-review: 0 inline findings).
        by_commit = {}
        for x in reviews:
            sha = (x["commit"] or {}).get("oid")
            by_commit[sha] = by_commit.get(sha, 0) + sum(1 for c in x["comments"]["nodes"] if not c["replyTo"])
        findings_by_round = list(by_commit.values())
        if summary_sha and summary_sha not in by_commit and findings_by_round:
            findings_by_round.append(0)
        answered = bool(requests and verdict_at and verdict_at > requests[-1]["createdAt"])
        return {
            "provider": self.name,
            "reviewed_sha": reviewed_sha,
            "critical_impact": bool(re.search(r"\[Critical (impact|risk)\]", impact_text, re.I)),
            "findings_by_round": findings_by_round,
            "present": bool(reviews or summary),
            "reviewed_head": reviewed_sha == head,
            "score": score,
            "rounds": len(requests),
            "retag_pending": reviewing or bool(requests and not answered
                                               and age_minutes(requests[-1]["createdAt"], now)
                                               < (timeout_min or self.timeout_min)),
            "retag_unanswered": bool(requests and not answered),
            "verdict_at": verdict_at,
        }


PROVIDERS = {p.name: p for p in (Greptile(),)}
# Inline findings from any of these count, whether or not the bot is the configured provider
# (extra findings only ever block more, so ship matches them loosely). `ship.finding_bots` adds
# other review bots (e.g. a security agent) by login substring.
FINDING_BOTS = tuple(l for p in PROVIDERS.values() for l in p.logins)


def is_review_request(body):
    """A comment that only asks a bot for a review (not the human's own words)."""
    return any(p.is_request({"body": body, "author": None}) for p in PROVIDERS.values())


def provider(name):
    """The configured provider, or None for "none". Unknown names are an error: a typo must not
    silently turn reviews off."""
    if name in (None, "", "none"):
        return None
    if name not in PROVIDERS:
        raise RuntimeError(f"unknown review provider {name!r} (have: {', '.join(PROVIDERS)}, none)")
    return PROVIDERS[name]
