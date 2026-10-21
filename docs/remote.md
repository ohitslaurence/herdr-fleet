# Remote access: an agent on your tailnet reading the fleet

`fleet remote serve` lets an agent running somewhere else (a Grok or Claude loop in a container,
another machine's assistant) see what the fleet is doing: whether the watcher is up, every PR
and session by section, and what's waiting in your inbox. It is **read-only** in this release:
nothing it exposes starts, prompts or steers an agent, merges, or changes config.

## The shape of it

```
 bot container                       your tailnet                      the fleet machine
 ┌───────────────────┐   tag:fleet-client  ┌──────────┐   tcp:7412 only   ┌──────────────────────┐
 │ agent loop (MCP)  │ ── tailscale ──────▶│   ACL    │ ────────────────▶ │ fleet remote serve   │
 │ + tailscale       │   sidecar           └──────────┘                   │  (Tailscale IP only)  │
 └───────────────────┘                                                    │      │ unix socket    │
                                                                          │  fleet watch (local)  │
                                                                          └──────────────────────┘
```

Two layers, both required:

1. **Network.** The bot's node joins the tailnet with an ephemeral auth key tagged
   `tag:fleet-client`. The ACL lets that tag reach the fleet machine on one port and nothing else:
   no SSH, no other ports, no other machines.
2. **Application.** `fleet remote serve` is a separate process that only talks to the watcher's
   local socket. It binds the machine's Tailscale address only, asks `tailscaled` who each caller
   is (`tailscale whois`, fail closed), and serves only the tools `[remote.scopes]` grants that
   caller's tags. Untagged nodes, people's devices and the fleet machine itself get nothing.

## Tools

| Tool | What it returns |
|---|---|
| `fleet_status` | watcher version, start time, last pass, whether herdr events are connected |
| `fleet_overview` | the cockpit: rows by section (NEEDS YOU, READY TO MERGE, IN FLIGHT, NO AGENT, IDLE), stale PRs only counted (`backlog_count`) |
| `fleet_inbox` | agents' questions and offers waiting for you (text capped) |
| `fleet_changes` | what changed since a time (`since`, ISO 8601; default the last 24 h): PR state changes with their reason, and new inbox items |
| `fleet_pr` | one fleet PR in full (`ref`: a row's `where` or `url`): description, changed files, the diff (first 24k characters), checks with links, reviews and comments (newest 40) |

Each `fleet_overview` row has: `section`, `kind` (pr or pane), `where` (repo#n or the agent),
`state` (ship's state), `detail` (what it is and the next step), `reason` (why it's in that state:
failing checks or the failed run's link), `url`, `agent`, `agent_status`, `needs_human`, `urgent`,
`critical` (the risk tier), `urgency` (0-3, higher sooner), `checked_at` (when ship last looked),
`pushed_at` (the last commit), `idle_days` and, for `ci-red` and `deploy-failed`, `failure_log`:
the last lines of the failed run's log (redacted; fetched once per run by ship on this machine,
not on the caller's demand). Fields a row doesn't have are left out.

Tools take no arguments, except `fleet_changes`'s optional `since` and `fleet_pr`'s required
`ref`. Arguments are checked against a per-tool allowlist (`TOOL_ARGS`): an unknown argument, a
`since` that isn't a real ISO 8601 time with a zone, or a `ref` that isn't `repo#n`,
`owner/repo#n` or a GitHub PR URL is refused with a 400.

Statuses: 400 means the request was wrong, 403 not allowed, 429 rate limited or busy (other calls
are already waiting on the same thing; retry shortly), 503 the fleet machine's side (the watcher
isn't running or didn't answer in time), 500 a bug (only the error's type is audited).

`fleet_pr` is the one tool that reaches GitHub. The remote process doesn't: it asks the watcher,
which looks the ref up in the cockpit's live rows (not the backlog `fleet_overview` only counts)
and refuses anything else, so a caller can't point `gh` at other repos. A short ref that two repos
share (`api#12` in a fork and upstream) is refused with both names: use `owner/repo#12`. It runs on
this machine's `gh` login, so it's bounded: one fetch at a time per PR (concurrent callers share
it), answers cached per head commit for 5 minutes and failures for 60 s, and at most 60 fetches an
hour in all (three `gh` calls each, the GraphQL `pr view` among them: a few percent of GitHub's
hourly budget, which your agents share); past that it answers from the cache or says the budget is
spent. A fetch waits in its own pool of the watcher's socket, so it can't hold up your own
refreshes. The answer is redacted before anything is cut, then capped (diff, body, 40 comments of
800 characters, 100 files).

Text that agents, PR authors or reviewers wrote comes back inside
`{"untrusted_content": true, "note": ..., "data": ...}`, redacted (keys, tokens, passwords,
private keys, webhook URLs: `bin/fleet_redact.py`) and with terminal control characters removed.
Redaction is best effort: a secret in a shape it doesn't know gets through, such as a bare 33 to
63-character hex key with no label (commit SHAs look the same), a single-case key like
`AGE-SECRET-KEY-1...`, or a short password in prose ("the password is hunter2"), so don't scope a
bot you wouldn't show your PRs to. It errs
the other way too: code that looks like a credential (`sessionKey = build(...)`, a long
camelCase name) is redacted, and a line over 8192 characters is replaced whole rather than
scanned, so one hostile line can't stall the machine.
The bot's prompt should treat it as data: an agent's pane can say anything.

Deliberately not remote: anything that runs `ship` or a model (cost on demand), `gh` beyond
`fleet_pr`'s cached fleet-only reads, refresh, and every write. Prompting an agent from outside
is code execution on this machine; it will come later only behind a per-message approval on your
phone (see Next).

## Endpoints

`POST http://<fleet-host>:7412/mcp`: MCP over streamable HTTP (JSON responses; `initialize`,
`tools/list`, `tools/call`, `ping`). `POST /v1/<tool>`: the same tools as plain JSON.

Every request needs the header `Fleet-Remote: 1`, a `Host` of the machine's Tailscale IP or
MagicDNS name with the port, and no `Origin` header. That combination is what a browser on one of
your own devices can't produce, which closes cross-site requests and DNS rebinding. Bodies are
capped at 16 KiB, a whole request must arrive within 10 s, at most 16 connections are open at once,
4 from one peer (more are closed on accept), each source gets 60 requests a minute before it's
identified, and each tag 30 calls a minute, shared by every node carrying it
(`remote.rate_per_min`; by tag so an ephemeral bot can't reset it by restarting). Text fields are
cut at 300 characters. A peer's identity is remembered for 30 s by its address (a tag you
remove takes effect within that, and a node handed a departed node's address within that window
would be taken for it). Refusals are audited up to 10 a minute per source, audit fields are cut at 200
characters (a made-up tool name is logged as `<unknown>`), and the audit log rotates at 5 MiB,
keeping 3 older generations.

## Setting it up

**1. Tailscale ACL** (admin console → Access controls). Name the fleet machine as a host, so you
don't have to retag it (retagging a personal device changes who owns it):

```jsonc
{
  "tagOwners": {
    "tag:fleet-client": ["autogroup:admin"]
  },
  "hosts": {
    "fleet-host": "100.x.y.z"   // the fleet machine's Tailscale IPv4 (`tailscale ip -4`)
  },
  "grants": [
    // ...your existing grants for your own devices...
    { "src": ["tag:fleet-client"], "dst": ["fleet-host"], "ip": ["tcp:7412"] }
  ],
  "tests": [
    { "src": "tag:fleet-client", "accept": ["fleet-host:7412"],
      "deny": ["fleet-host:22", "fleet-host:443", "fleet-host:8080"] }
  ]
}
```

This only isolates the bot if nothing else in the policy grants `tag:fleet-client` (or `*`)
wider access: if your tailnet still has the default allow-all rule, replace it with explicit
grants for your own devices first. The `tests` block makes Tailscale refuse to save a policy that
would let the tag reach SSH or anything but the one port.

**2. An auth key for the bot**: Settings → Keys → Generate auth key: *ephemeral*, *pre-approved*,
tags `tag:fleet-client`, short expiry. Ephemeral nodes disappear when the container stops.

**3. Allow the tag** in `~/.config/fleet/config.toml`:

```toml
[remote.scopes]
"tag:fleet-client" = ["fleet_status", "fleet_overview", "fleet_inbox", "fleet_changes", "fleet_pr"]
```

Changes to `[remote.scopes]` and `remote.rate_per_min` apply from the next request: the server
re-reads the config when the file changes, so revoking a bot is an edit, not a restart. An edit that doesn't parse, or gives any `remote.*` setting a bad value, keeps the last good
config in full (the journal says why) until the next good write; a revocation in that same edit
waits for it too, so check `fleet remote logs` after revoking. `fleet remote serve` won't start
on a `[remote]` it can't use.

**4. Run it** on the fleet machine: `fleet remote install` (a user service like the watcher's:
it shows the unit, then asks; `fleet remote status|logs|restart|uninstall` after), or
`fleet remote serve` in a pane to try it. `fleet remote scopes` shows what's allowed, `fleet
remote audit` every call with who made it and whether it was refused.

**5. The bot**: a container with a Tailscale sidecar using that key, and an agent loop whose MCP
config points at `http://fleet-host:7412/mcp` (or the MagicDNS name) with the header
`Fleet-Remote: 1`. Hosted chat products can't join your tailnet; don't expose the port with
Tailscale Funnel, which would put it on the public internet.

## What this protects against, and what it doesn't

- A compromised or prompt-injected bot can read your fleet's state and inbox (PR titles, agent
  questions, repo names) and, with `fleet_pr`, the diffs and review comments of your fleet's live
  PRs. Scope it accordingly. It cannot change anything, reach a shell, read files, or make this
  machine call GitHub for anything but those PRs, within the hourly budget.
- Any peer the ACL lets reach the port can hold 4 connections before it's identified; the ACL
  (only `tag:fleet-client` reaches the port) is what keeps everyone else off it.
- Devices you own and the fleet machine itself are refused even though they're on the tailnet,
  so a web page you open can't use your browser to call the API.
- Every call is audited to `~/.local/state/fleet/remote-audit.jsonl` (owner-only) and the journal.
- The systemd unit `fleet remote install` writes runs it with no privilege gain
  (`NoNewPrivileges`), only Unix and IP sockets, the `@system-service` syscall set minus
  `@privileged` and `@resources`, and a private umask. It still runs as you and can read what you
  can: filesystem sandboxing (`ProtectSystem`, `ProtectHome`) needs user namespaces, which user
  units on hosts like Ubuntu 24.04 don't get, and systemd ignores it there silently, so it isn't
  claimed. What keeps files out of reach is the code: no tool reads a file or runs a command on a
  caller's behalf. On macOS (launchd) none of this applies.
- Responses don't name the Python version (`Server: fleet-remote`).

## Next

Gated prompts (a bot asks to send an agent a message; you approve that exact text, for that pane,
once, on an authenticated channel; it expires), and a red-team pass on them.
