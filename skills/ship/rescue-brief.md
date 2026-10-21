# Rescue: {{PR_URL}}

GOAL
Get this PR to a clean, mergeable state. It is stuck: each round of review fixes has produced new
findings (findings per paid Greptile review, oldest first: {{HISTORY}}). The previous {{AUTHOR}}
agent's context was full of its own patches; you start fresh and from a different model family on purpose.

SCOPE
Branch `{{BRANCH}}` (base `{{BASE}}`). Your worktree is {{WORKTREE}}: `git fetch origin && git checkout -B {{BRANCH}} origin/{{BRANCH}}`.
The previous agent has been told to stop; you own the branch now.

DO
1. Read the PR description and the full diff against `origin/{{BASE}}` as if seeing it cold. Read every
   review comment (Greptile and cross-family) to learn what kept going wrong. Don't fix them one by one yet.
2. Diagnose why it isn't converging. Usual causes: the design fights the codebase, the change is too big
   to review, the fixes are patches on symptoms, or a wrong assumption runs through it.
3. Choose and record your choice in {{REPORT}} first:
   a. fix: the design is sound; fix the root causes, not the individual comments
   b. simplify: rewrite the messy part more simply on the same branch
   c. split: the PR is too big; propose the split (needs the user: append `needs-decision:` and stop)
4. For a or b: implement, run the repo's checks, push, then follow `ship guide loop`
   (`ship --act --wait` loop, including `ship guide self-review`).

FORBIDDEN
Merging by hand, force-pushing over someone else's newer commits without reading them, widening scope.

REPORT
Append status lines to {{STATUS}}: `working: …`, then a terminal one (`done:`, `needs-decision:`, `blocked:`, `failed:`).
Write {{REPORT}}: diagnosis, chosen path, what you changed, final `ship` state.
