---
name: ship
description: Take a PR from open to merged and deployed without the user steering each step, using the `ship` CLI. Use when the user says ship it, take it e2e, get this merged, PR please and see it through, babysit/finish the PR, or after opening a PR when the task is not done until it lands.
argument-hint: "[<pr-url-or-number>]"
---

# Ship a PR

`ship` reads the PR's whole state from GitHub and prints ONE next action. You do
that action, then ask `ship` again. The loop ends only at a terminal state.

## Loop

1. No PR yet: commit, push and open it as a draft. With gritty: `gritty commit --accept`,
   `gritty pr --accept --draft`. Without: `git commit`, `gh pr create --fill --draft`.
   ship marks it ready once it's clean, so paid reviewers only see the finished work.
2. Run `ship --act --wait` (add the PR URL if you are not on its branch).
   `--act` does the mechanical steps itself: request a Greptile review when the
   budget allows, re-run cancelled CI, update a behind branch, mark ready,
   label, and merge when policy allows. `--wait` blocks while CI runs, Greptile
   reviews, or the deploy rolls out, so you don't poll.
   If the user says they merged it, or asks whether it's open, merged or deployed:
   run `ship pr <ref>` (about 1 s, read-only) and answer from that. Use `--wait` only
   when it shows a deploy still running.
3. Do what `next:` says. The states that need code:
   - `conflicts`: rebase onto the base, resolve, run the repo's checks, push `--force-with-lease`.
   - `ci-red`: read the failing job logs (`gh run view <id> --log-failed`), fix the cause, push.
   - `review-findings`: work the findings (below), push.
   - `self-review`: write the self-review (below) before the next review.
   - `cross-review`: just run `ship --act --wait`; it runs the free review.
   - `craft-review`: just run `ship --act --wait`; it runs the quiet craft review.
   - `craft-findings`: give every finding a verdict (below).
4. Go back to step 2.

## Re-reviews cost money: earn them

Every Greptile re-review is paid. `ship` decides when one is worth it from the
PR's tier (`ship policy` shows the numbers):

- **normal** PRs pass at 4/5. Minor (P2) findings get fixed or answered and
  the PR moves on with no re-review, as long as the fix is small.
- **critical** PRs (payments, ledger, auth, KYC, migrations, infra, or
  money-moving repos) need 5/5, and any real change after the last review gets
  re-reviewed, up to a budget. Then a human decides.

Our own models are free to use (subscriptions), so `ship` spends them first:

- **cross-review**: a fresh-context review by the *other* model family (Codex
  reviews Claude's PR and vice versa) runs headless on the PR head. It is
  required before every paid re-review on critical PRs, and on any PR once a
  paid re-review has failed to converge. `ship --act --wait` runs it and posts
  the verdict; treat its findings exactly like Greptile's.
- **churn**: if each paid review finds as many problems as the last, the fixes
  are creating new bugs and your context is the problem. `ship` says `churn`:
  run `ship rescue`, which hands the PR to a fresh agent from the other family,
  then STOP working on the PR. Don't keep patching.

Never re-tag Greptile yourself (`@greptileai`). Only `ship --act` does, and
only when the budget and the self-review allow it.

## Rescue

When `ship` reports `churn`, run `ship rescue [<pr>]`. It hands the PR and the
review history to a fresh agent from the other model family. Once the handoff
succeeds, stop editing the branch and report the handoff. If rescue fails, report
the error; do not claim another agent owns it until the command succeeds.

## Working findings: the thinking is the job

A reviewer's comment points at a symptom. Fixing exactly the line it names is
the lazy move, and it costs a paid re-review per round when the reviewer finds
the same mistake one function over. Instead, for each finding:

1. **Decide whether it's real.** Read the code around it. Greptile lacks
   codebase context and is often wrong; when it is, reply with the specific
   reason (`ship guide replies` covers reactions and replies) and
   move on. Don't "fix" a non-bug to make the reviewer happy.
2. **Name the cause, not the line.** What wrong assumption produced this? For
   example, "this code assumes the webhook arrives once", not "missing
   idempotency check on line 40".
3. **Find the siblings.** Search the diff and the code it touches for the
   same assumption (`rg`). Fix every instance, not just the one flagged.
4. **Prove it.** Add or extend a test that fails without the fix whenever the
   finding is about behaviour. Run the repo's checks.
5. **Pre-empt the next round.** Before pushing, reread the full diff the way
   the reviewer will: what would it flag next? Fix that now.

For minor findings (naming, a missing test case, a doc line): fix them
properly, reply on the thread, and don't ask for a re-review.

## Self-review (required before a paid re-review)

When `ship` says `self-review`, post one PR comment (`gh pr comment`) that ends
with the marker line, using the current head SHA (`git rev-parse HEAD`):

```
Self-review before re-review

Findings addressed:
1. <finding> → cause: <the wrong assumption> → fixed in <files>; siblings checked: <where> (<n> more fixed | none)
2. ...
Dismissed: <finding> — <why it isn't a bug>
Tests: <what now covers it>; checks: <command> passing
Risk I still see: <one line, or "none">

<!-- fleet-self-review sha=<HEAD> -->
```

Write it from the diff, not from memory. If you can't fill a "cause" or
"siblings checked" line honestly, you haven't finished: go back to the
findings. Then run `ship --act --wait`.

## Craft review

Where the repo turns it on (`ship.craft_review`), a fresh reviewer that didn't write the code
(the other model family when it can run sandboxed) reads the draft once for craft (with `merge = "auto"`, a PR opened ready too): architecture fit, readability, test quality, PR clarity, and anything
that didn't need to exist. Its findings stay on this machine, never on the PR. `ship findings`
lists them.

Judge each one like any review comment (`ship guide findings`): is it real here, in this repo's
conventions? Fix the real ones properly; dismiss the rest with the reason. Then record it:

```
ship findings c1 c3 fixed "split parse_args out of main; renamed x to retries"
ship findings c2 dismissed "the two loops differ on purpose: one must not block on GitHub"
```

Be honest in both directions. The verdicts are how the fleet learns: findings that keep getting
fixed become lint rules and agent instructions, and findings that keep getting dismissed teach
the reviewer what to stop raising. Dismissing a real finding to move on teaches it the wrong thing.
Once every finding has a verdict, `ship --act --wait` continues and marks the PR ready.

## Stop only at a terminal state

| state | what you do |
|---|---|
| `deployed` | If `next:` says to run after_deploy, run `ship --act` first (your config's post-deploy command; it runs once). Tell the user in one line: PR, merged, deployed. Then stop. Don't run or offer `dev done`: it closes this pane and worktree, and the user does that from the cockpit. |
| `awaiting-human` | Clean and green, but policy needs a human merge. One line: PR URL + why. |
| `needs-human` | Budget spent or review didn't converge. Summarise what's still open in ≤5 lines and ask one question. |
| `deploy-gated` | A deploy waits for environment approval. Say so with the run URL. |
| `deploy-failed` | Investigate the failed run; report. Never push to main to fix it. |
| `closed` | Say so. |
| `churn` | Run `ship rescue`, tell the user one line (PR, handed to a fresh agent), and stop. |

## Rules

- Do not ask the user before any step in this loop that `ship` tells you to do. Those steps are reversible and in scope. Ask only at `needs-human`.
- Never merge by hand with `gh pr merge`; only `ship --act` merges, because it applies the policy.
- If the same non-terminal state repeats three times with no progress, stop and report it as `needs-human`.
