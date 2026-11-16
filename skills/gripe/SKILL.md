---
name: gripe
description: Log friction you hit while working, in one command, then carry on. Use whenever something cost you time or confused you - unclear or wrong docs, a tool or CLI that fought you, a flaky test, a missing script, a codebase convention you had to guess, an instruction that contradicted another, a permission or hook that blocked you for no clear reason. `fleet gripe "<what went wrong>"` takes seconds; the user reads them to fix what keeps hurting. Don't ask first, don't stop your task for it.
metadata:
  version: "{{FLEET_VERSION}}"
---

# Gripe

The user wants to know where work is harder than it should be. You notice it first, so log it
the moment it happens, then carry on with your task:

```bash
fleet gripe "<what went wrong, one or two sentences>" \
  [--tried "<what you did about it>"] [--cost 10m] [--fix "<what would have helped>"] \
  [--about <the tool, doc, file or command>]
```

- Say what happened concretely: the command, file or doc, and what was wrong or missing.
  "ship pr said CI green while two checks were still queued", not "ship is confusing".
- `--about` groups gripes: use the tool or command name (`ship`, `orch wait`, `dev`) or the file
  (`docs/deploy.md`). Without it, gripes group by repo.
- `--fix` is the most useful part when you have one: the doc line, flag or check that would have
  saved you the time.
- Pane, agent kind, repo, branch and orch worker name are recorded for you.
- It stays on this machine and secrets are redacted, but don't paste secret values, customer
  data or whole logs: quote the one line that matters.

When: anything that cost you more than a few minutes or a wrong turn, or that you'd warn the next
agent about. One gripe per problem; don't log the same thing twice in a session. Not for task
status (that's `fleet note` and orch status lines) or for questions you need answered (ask).

Reading them: `fleet gripes [--since 7d] [--all] [--json]` lists them grouped by topic, most
frequent first.
