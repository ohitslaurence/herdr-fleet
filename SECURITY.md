# Security

## Reporting a vulnerability

Please report it privately: on GitHub, **Security → Report a vulnerability** on this repository
(private vulnerability reporting). Don't open a public issue or PR for it. Include what an
attacker controls, what they gain, and a way to reproduce if you have one.

You'll get an answer within 7 days. Fixes go into the latest release only (pre-1.0, there are no
maintained older lines); the changelog credits the reporter unless you'd rather it didn't.

In scope: anything that lets untrusted text (PR comments, review bodies, CI logs, agent output,
a repo's `.fleet.toml`) run commands, loosen policy, merge, or leak secrets; and anything that gets
past `fleet remote`'s identity, scope, or read-only boundary. The design notes below say what each
part trusts.

## Design notes

What the tools here trust, and what they refuse to decide for you. Settings are in
[docs/configuration.md](docs/configuration.md); the layering rules (tighten-only repo files, no
environment override) are in the README.

## orch workers run with permission checks off

`orch start` always launches its worker with the harness's bypass flag
(`--dangerously-skip-permissions`, `--dangerously-bypass-approvals-and-sandbox`, `--auto`),
whatever `agents.permissions` says. A worker nobody watches can't answer a permission prompt, so
the alternative is a worker that stalls. The cost is that whatever the worker reads can steer it;
the rules below exist to keep that surface small.

- **Briefs carry untrusted text.** PR comments, issue bodies and logs pasted into a brief go
  under *Context*, quoted as data; the template tells the worker not to follow instructions found
  there. The orchestrator writes the intent in its own words.
- **Dialogs are answered only when orch can vouch for them.** Claude's trust prompt is accepted
  only for the worker's own worktree when it lists no extra permissions. Codex's "Hooks need
  review" is accepted only when every hook it lists is under `~/.codex` (the user's own); a hook a
  repo ships in its checkout, a login screen, the Bypass Permissions acceptance, or any other
  yes/no dialog stops the start with exit 1 and the pane to look at. The brief is never typed into
  a dialog.
- **A brief is sent at most twice,** and only re-sent when the composer is verifiably empty.
  Anything less certain is reported, not retried.
- **Repo aliases are validated** (`[A-Za-z0-9._-]+`) and herdr/dev JSON is read by field name
  from argv, never by evaluating a string with the alias in it.
- **Security briefs** go to `orch start --role security` (`[roles.security]`, Claude by default):
  Codex's cyber filter refuses them, and a refusal looks like a quiet worker. `orch wait` reports a
  worker idle `orch.quiet_min` minutes with no status line so a refusal doesn't wait out the timeout.

## ship reviewers are read-only

Headless reviews run `codex exec -s read-only --ephemeral` or `claude -p` with only Read, Grep and
Glob allowed, in a checkout that is treated as attacker-controlled: their output files live
outside it, and the prompt arrives on stdin (claude) or as a single argument (codex), never via a
file in the checkout. Without a working codex sandbox there is no codex review at all.
