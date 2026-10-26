# Changelog

`fleet update` shows the sections newer than your install before it asks. Versions: new commands
or settings bump the minor version, fixes only the patch. To cut a release: set `version` in
`herdr-plugin.toml`, add its section here, update the tag in install.sh's header and in README's
`git clone --branch` and `herdr plugin install --ref` lines (a test checks all of them), merge,
then tag the merge commit `v<version>` (annotated) and push the tag.

## Unreleased

- New setting `[dev] warm_caches`: gitignored build-cache dirs (e.g. `[".cache/typecheck"]`)
  that `dev` copies into a new worktree from the sibling that wrote them last (`fleet new`'s
  spares re-copy on every refresh). A cold typecheck can take 5x the memory of a warm one; a fleet starting a dozen cold at
  once ran a 32 GB machine out of memory.
- Fix: `fleet remote`'s PR ref shape check was Unicode-wide (`\w`); it is ASCII now. Harmless
  (the watcher matches refs exactly, so a lookalike never fetched), tightened on review.
- Tests: adversarial edge cases for `fleet remote` (MCP notifications never execute, id 0 is a
  request, absolute-form targets, duplicate Content-Length, strict Host shapes, per-tag budget
  shared by ephemeral nodes, the whois cache's stale-tag window closing).

## v0.4.0 (2026-10-21)

The repository is now public; earlier versions were private.

- Workers report back. `orch wait` returns as soon as one of your workers finishes, instead of
  holding every finished report until the last worker is done (`--all` keeps the old behaviour),
  and with no names it waits only for the workers your pane started. When the orchestrator has
  ended its turn, `fleet watch` prompts it with the names of workers that finished.
- Fix: `orch start` marked Claude workers failed ("brief not confirmed") while they worked on the
  brief: Claude no longer prints "esc to interrupt" and keeps its composer on screen. herdr's
  working status now confirms the brief.
- The `orchestrate` skill now chooses a harness, model and effort per worker: by the model's
  strengths, by what a wrong answer would cost, and with second opinions from another harness.
  Before, every worker took the `worker` role's default.
- `orch start --model <nickname>` uses your `[models.<nickname>]` entry (its harness and model).
- `orchestrate` is a skill of its own again (`/orchestrate`, with its brief template): folding it
  into the fleet skill had made it disappear for agents that loaded it by name. `fleet skills
  install` copies it beside `fleet`; `fleet update` adds it to installs from before.
- Fix: `t` in the cockpit closes the cockpit once `fleet new` has taken you to the session (as
  Enter does after a jump). The popup cockpit stayed open behind the new session, and herdr then
  refused every other popup: `ctrl+w f` and `ctrl+w t` did nothing. Cancelling keeps you in it:
  `fleet new` now exits 3 when you cancel and nothing was started (was 0).
- Fix: `fleet new` takes you to the new session's agent tab. It asked herdr to focus the agent
  right after typing its start command, before herdr knew an agent was there, and ignored the
  failure; now it focuses the workspace and the `agent` tab, and says so if that fails.
- **Breaking:** inline findings from a `strix` bot no longer block a merge by default. To keep
  that, set `ship.finding_bots = ["strix"]`: other review bots (login substrings) whose findings
  block like the review bot's. A repo's `.fleet.toml` may add to it. The `prc` skill reads it too.
- Cross-family reviews ship posted before 2026-10-13 (before it kept a ledger of what it posted)
  no longer count as this PR's cross review; ship asks for a fresh one if the policy needs it.
- Ready to publish: the real install URL, a vulnerability reporting policy in SECURITY.md, and
  no references only the author could follow.
- macOS: with Apple's Python 3.9 first on PATH, every command says to put a newer `python3`
  first (one line, no traceback); `orch` says it needs bash 4 (macOS ships 3.2), and
  `fleet doctor` checks for it; `orch --help` works with BSD sed. CI runs on macOS too.
- For contributors: CONTRIBUTING.md, issue and PR templates, CODEOWNERS; CI runs on Python 3.11
  and 3.13, actions pinned to commits (Dependabot keeps them current);
  the test suite closes everything it opens (no ResourceWarnings).
- `ship.after_deploy`: a command `ship --act` runs once when a PR is deployed (in `~`, 10 min
  cap), e.g. reinstalling a tool you run from main. Your config only: a repo's `.fleet.toml`
  can't set it.
- ship runs the craft review on a PR opened ready, not only on drafts, when the repo's merge
  policy is `auto`: nobody else reviews it there, so it no longer merges on green CI alone.
- orch's done check names untracked files apart from changes, and no longer reports a branch
  that was pushed, merged and pruned as unpushed. Every tool turns checkout hooks off through
  one `fleet_paths.NO_HOOKS`, and a test fails any new worktree call that doesn't.
- `orch wait` and `orch report` check a worker's `done` against its worktree: uncommitted
  changes or commits no remote has are named (`[w] done, but its worktree: 2 commits not pushed`),
  so an orchestrator doesn't archive a PR that was never pushed.
- Fix: `fleet new` and `dev` started no agent in a repo whose git checkout hook fails before its
  dependencies are installed (husky's `yarn yarnhook`): git exited 1 with the worktree made, and
  the session stopped before its `shell` and `agent` tabs. Worktrees (dev, fleet new's spares,
  orch, ship's review checkouts) are now created with checkout hooks off; the install runs as
  before. The cockpit's `t` keeps a failed `fleet new` on screen instead of clearing it.
- Remote access, hardened (red-teamed until no findings were left):
  - Redaction (`fleet_redact`, used everywhere text leaves the machine or reaches a log): more
    formats (AWS secret keys, quoted JSON/YAML keys, PGP keys, Slack/Discord webhooks, GitLab,
    Google and npm tokens, Basic auth, `curl -u`, `mysql`/`sshpass -p`, long values after a key
    word, bare 32-character hex), runs before any text is cut, takes linear time on any input
    (lines over 8192 characters are replaced whole), and leaves `passengers`, `tokenizer` and
    `docker run -u` alone. Every remote tool's output goes through it.
  - `fleet_pr`: live rows only; `owner/repo#n` refs, and a short ref two repos share is refused
    naming both; one fetch per PR at a time, failures cached 60 s, at most 60 fetches an hour;
    its own watcher pool, so it can't hold up refreshes.
  - The API: statuses say whose problem it is (400 the request, 429 busy or over budget, 503 the
    watcher or GitHub); 4 connections per peer; identities remembered 30 s; no request waits past
    its deadline; audit fields capped and 3 older generations kept; config edits apply without a
    restart, swapped in atomically, and an edit that doesn't parse keeps the last good config; no
    Python version in `Server`.
  - The remote service unit runs with `NoNewPrivileges` and socket-family and syscall filters.
  - Internal: `fleet_serve.OpError` lets an op refuse with a code; `fleet_serve.SLOW_OPS` is a
    pool size per op; `fleet_state.append_jsonl(keep=)`; `fleet_config.build()`/`reload()`.
- `fleet_pr` (remote API): one fleet PR in full, for a bot that has no GitHub access of its own:
  description, changed files, the diff (capped), checks with links, reviews and comments. The
  watcher fetches it with this machine's `gh`, only for PRs in the cockpit, cached per commit and
  redacted. Add it to a tag's `[remote.scopes]` to allow it.
- Why, not just what: a PR that goes `ci-red` or `deploy-failed` gets the failed run's last log
  lines (fetched once by ship, redacted) as `failure_log` on its row. Every PR state change and new
  inbox item is appended to `~/.local/state/fleet/events.jsonl`; the remote tool `fleet_changes`
  returns what changed since a time, for briefings. It's the first remote tool with an argument
  (`since`, validated). `fleet_overview` rows now omit fields that are empty rather than sending null.
- `fleet new` starts the agent idle: your sentence names the session and sets its note, and you
  give the first instruction. `p` in the preview, `--prompt`, or `new.send_sentence = true`
  start it on the sentence as before.
- `fleet_overview` (remote API) rows carry `reason`, `critical`, `urgent`, `needs_human`,
  `checked_at`, `pushed_at`; renamed `state`, `agent_status`, `idle_days`; the backlog is counted,
  not listed. `fleet ls --json` gains the same fields and loses the ‼ in `detail`.

## v0.3.0 (2026-10-19)

- `fleet remote` hardened after a red team: an absolute 10 s deadline per request, connections
  capped at accept, strict Content-Length, a per-source limit before identification, rate limits
  by tag (an ephemeral bot can't reset them), capped text fields, bounded and rotated audit
  logging, and answers (not dropped connections) for malformed MCP.
- `fleet remote install|uninstall|start|stop|restart|status|logs`: the read-only tailnet API as a
  user service, like the watcher; `fleet update` restarts every running fleet service.
- `remote.rate_per_min` now applies per Tailscale tag, shared by every node with that tag.

## v0.2.0 (2026-10-19)

First version meant for someone other than its author.

- **Install, update, uninstall.** `install.sh` links the commands and the herdr plugin after
  showing every change, or `herdr plugin install` does it all (the installer is its previewed
  build step); `fleet update` moves between release tags only, showing the changelog and the
  full diff of skills and installer first; `fleet uninstall` removes exactly what install added.
- **The fleet skill.** `fleet skills install` puts a version-stamped copy of one skill into each
  agent harness it finds (Claude Code, Codex, opencode, and the shared ~/.agents folder): rules first, then every command by kind.
- **Checks itself.** `fleet doctor` checks the whole install in about half a second and gives
  each problem its fix and who may run it. `fleet version`.
- **First config from the machine.** `fleet setup --propose` finds your repos folder, orgs,
  worktree and branch habits and agent CLIs, with evidence; it never proposes looser safety.
  `fleet setup apply` validates and writes only after a yes on the terminal. Unknown or
  misspelt settings now warn, invalid choices fail.
- **Keys.** `fleet keys status|set|unset|test`: hidden prompt, owner-only file, never printed.
- **The watcher as a service.** `fleet server status|start|stop|restart|install|uninstall|logs`
  for systemd and launchd, with PATH captured from your login shell.
- **Quiet craft review.** With `ship.craft_review`, a fresh reviewer reads each draft PR once
  for architecture, readability, tests, clarity and excess. Findings stay local; the agent
  marks each fixed or dismissed before the PR goes to paid reviewers. `fleet learn` turns those
  verdicts into proposed rules, lints and tests.
- **Remote, read-only.** `fleet remote serve` lets an agent on your tailnet read the fleet
  (status, the cockpit, the inbox) over MCP or JSON: Tailscale address only, whois identity,
  per-tag scopes, browser-proof, rate-limited and audited. docs/remote.md has the ACL.
- **Repos from config.** `[dev] repos_dir` and `[dev.repos.<alias>]` replace `DEV_REPOS_DIR`
  and `repos.tsv` (no fallback). The Node `dev.js` is gone.
- **Agents.** `fleet new` starts any configured harness (claude, codex, opencode, your own) and
  model, and says when it fell back to rules. `orch` refuses unfilled briefs, stops on dialogs
  it can't answer, reports quiet workers and has `--json` status.
- **Safety.** Safe defaults for anyone who hasn't chosen; the fleet socket is owner-only and
  limited; GraphQL queries take variables; untrusted text can't drive the terminal; inbox items
  expire; failures say so (`herdr unreachable`, one-line config errors) instead of looking like
  an empty fleet; usage errors exit 2 everywhere.

## v0.1.0 (untagged)

The author's working set: ship, the cockpit, fleet new, fleet watch, dev and orch.
