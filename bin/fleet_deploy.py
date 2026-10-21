"""fleet_deploy — deploy providers: what ship watches after a PR merges.

`[ship] deploy = "<name>"` (or "none"); repos override with `[repos.<name>] deploy = ...`.
status(o, r, sha, merged_at, base) -> (state, detail), state one of:
  deploying      runs in progress, or merged moments ago and runs haven't started yet
  deploy-gated   a run waits for an environment approval
  deploy-failed  a run failed
  deployed       every run that started succeeded, and nothing expected is missing
  needs-human    something expected didn't happen: an always-run workflow never started, or a
                 run has been going far longer than deploys take

"actions" (GitHub Actions) learns what to expect from the base branch's history: workflows that
ran on every one of the last few pushes to it. "Nothing started within a minute" used to count
as deployed; now it only does when the base branch has no such always-run workflows.
"""
import json, os, sys, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import fleet_proc as proc  # noqa: E402

START_MIN = 5          # runs start within seconds of a merge; after this, missing ones aren't coming
TIMEOUT_MIN = 90       # a deploy run still going after this long needs a look
HISTORY_PUSHES = 5     # "always runs" = ran on each of the last N pushes to the base
EVENTS = ("push", "workflow_run", "deployment")


def age_minutes(iso):
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 60


gh_json = proc.gh_json


class Actions:
    name = "actions"

    def __init__(self, state_dir, start_min=START_MIN, timeout_min=TIMEOUT_MIN, history=HISTORY_PUSHES):
        self.state_dir, self.start_min, self.timeout_min, self.history = state_dir, start_min, timeout_min, history

    def expected(self, o, r, base, sha, before=None):
        """Workflows that ran on each of the last HISTORY_PUSHES pushes to base before `before`
        (the merge time; a workflow added since doesn't count) other than sha. The push history
        is cached an hour. None when history is too short or GitHub can't say."""
        cache = os.path.join(self.state_dir, "deploy-expected.json")
        try:
            with open(cache) as f:
                c = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            c = {}
        key = f"{o}/{r}@{base}"
        hit = c.get(key)
        if hit and time.time() - hit["at"] < 3600:
            runs = hit["runs"]
        else:
            runs = gh_json("run", "list", "-R", f"{o}/{r}", "--branch", base, "--event", "push", "-L", "100",
                           "--json", "workflowName,headSha,createdAt")
            if runs is None:
                return None
            c[key] = {"at": time.time(), "runs": runs}
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            tmp = cache + ".tmp"
            with open(tmp, "w") as f:
                json.dump(c, f)
            os.replace(tmp, cache)
        by_sha = {}
        for x in sorted(runs, key=lambda x: x["createdAt"], reverse=True):
            if x["headSha"] != sha and (not before or x["createdAt"] < before):
                by_sha.setdefault(x["headSha"], set()).add(x["workflowName"])
        recent = list(by_sha.values())[:self.history]
        return set.intersection(*recent) if len(recent) >= self.history else None

    def status(self, o, r, sha, merged_at, base):
        runs = gh_json("run", "list", "-R", f"{o}/{r}", "--commit", sha, "--json",
                       "name,status,conclusion,url,event,createdAt", "-L", "50")
        if runs is None:
            raise RuntimeError(f"can't list workflow runs for {o}/{r}@{sha[:7]}")
        runs = [x for x in runs if x["event"] in EVENTS]
        young = bool(merged_at) and age_minutes(merged_at) < self.start_min
        # a cancelled run superseded by a later run of the same workflow isn't a failure
        newest = {}
        for x in runs:
            if x["name"] not in newest or x["createdAt"] > newest[x["name"]]["createdAt"]:
                newest[x["name"]] = x
        runs = list(newest.values())
        failed = [x for x in runs if x["status"] == "completed" and x["conclusion"] not in ("success", "skipped", "neutral")]
        if failed:
            return "deploy-failed", "; ".join(f"{x['name']}: {x['conclusion']} {x['url']}" for x in failed)
        gated = [x for x in runs if x["status"] == "waiting"]
        if gated:
            return "deploy-gated", "awaiting environment approval: " + "; ".join(f"{x['name']} {x['url']}" for x in gated)
        running = [x for x in runs if x["status"] != "completed"]
        slow = [x for x in running if age_minutes(x["createdAt"]) > self.timeout_min]
        if slow:
            return "needs-human", (f"deploy still running after {self.timeout_min}+ min: "
                                   + "; ".join(f"{x['name']} {x['url']}" for x in slow))
        if running:
            return "deploying", ", ".join(x["name"] for x in running)
        if young:
            return "deploying", "merged moments ago; waiting for post-merge workflows to start"
        expected = self.expected(o, r, base, sha, merged_at) if base else None
        missing = sorted((expected or set()) - {x["name"] for x in runs})
        if missing:
            return "needs-human", (f"{', '.join(missing)} ran on each of the last {self.history} pushes to {base} but "
                                   f"never started on this merge ({self.start_min}+ min)")
        if not runs:
            return "deployed", f"no workflow runs on the merge commit, and none run on every push to {base}"
        return "deployed", ", ".join(x["name"] for x in runs)


class Off:
    name = "none"

    def status(self, o, r, sha, merged_at, base):
        return "deployed", "merged (deploy watch off)"


def provider(name, state_dir, settings=None):
    """settings: the ship policy (deploy_start_min, deploy_timeout_min, deploy_history_pushes)."""
    settings = settings or {}
    if name in (None, "", "none"):
        return Off()
    if name == "actions":
        return Actions(state_dir, settings.get("deploy_start_min", START_MIN),
                       settings.get("deploy_timeout_min", TIMEOUT_MIN),
                       settings.get("deploy_history_pushes", HISTORY_PUSHES))
    raise RuntimeError(f"unknown deploy provider {name!r} (have: actions, none)")
