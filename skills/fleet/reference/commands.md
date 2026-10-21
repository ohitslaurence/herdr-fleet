# Commands: flags, exit codes, JSON

Exit codes: 0 ok · 1 failed or unhealthy · 2 usage (or "needs a terminal") · 3 declined at a
prompt. `-h`/`--help`/`help` anywhere prints help and does nothing else. `--json` prints one
object: read its fields, don't parse the text.

## fleet

| Command | Kind | Notes |
|---|---|---|
| `fleet ls [--refresh] [--all] [--json]` | read | Sections NEEDS YOU, READY TO MERGE, IN FLIGHT, NO AGENT, IDLE, BACKLOG. Exit 1 with `herdr unreachable: …` when herdr doesn't answer. `--refresh` re-runs ship over every PR (slow). |
| `fleet inbox [--json]`, `fleet inbox done <id>` | read / write | Close only items you resolved. Never `done --all`. |
| `fleet note ["<sentence>"]` | write | No argument prints the current note. |
| `fleet why [<pane\|repo#n\|point>] [-n N]`, `fleet why --stats <point>` | read | The decision log. |
| `fleet config show [key] [--repo owner/name[@base]] [--json]`, `fleet config get <section.key>` | read | Every value and the layer that set it. |
| `fleet role <role> [--author claude\|codex] [--json]`, `fleet roles` | read | `<kind> <model\|-> <effort\|->`. |
| `fleet doctor [--json] [--only id,…]` | read | `{version, ok, checks:[{id, status, summary, fix?, who?, detail?}]}`; exit 1 iff a check failed. |
| `fleet version [--json]` | read | `{version, commit, install, source}`. |
| `fleet setup --propose [--json]` | read | `{proposed, evidence, ask, toml, diff, never_proposed}`. Writes nothing. `proposed`: only the settings it would add; `toml`: the whole file as it would be after; `diff`: what changes (empty: nothing to add). |
| `fleet setup apply <file\|->`, `fleet setup` | human | Validates first (exit 1 on errors), then diff + confirm on a terminal. |
| `fleet keys status [--json]`, `fleet keys test [NAME] [--json]` | read | Names, presence and file mode. Never values. |
| `fleet keys set\|unset <NAME>` | human | Terminal only, hidden input. |
| `fleet server status [--json]`, `fleet server logs [-n N]` | read | The watcher: running, pid, version, last pass, herdr events, unit. |
| `fleet server start\|stop\|restart` | ask first | Through the installed unit. |
| `fleet server install\|uninstall` | human | Shows the unit file, then asks. |
| `fleet learn [--repo r] [--days N] [--json]` | read | What craft reviews found and what agents did with it. |
| `fleet learn --propose` | ask first | One model call; proposals are saved, nothing applied. |
| `fleet skills status [--json]` | read | Which agent harnesses have this skill, and at which version. |
| `fleet skills install [--to …]`, `fleet update`, `fleet uninstall` | human | Each shows what it changes, then asks. `fleet update --check` is read-only. |
| `fleet remote scopes`, `fleet remote audit [-n N]`, `fleet remote status`, `fleet remote logs` | read | Which tailnet tags may read the fleet remotely, every remote call, the service. |
| `fleet remote serve`, `fleet remote install\|uninstall` | human | Opens the read-only tailnet API (docs/remote.md), in a pane or as a service. |
| `fleet remote start\|stop\|restart` | ask first | The remote API service. |
| `fleet new "<repo>, <what>" [--yes] [--dry-run] [--agent K] [--model M] [--prompt\|--no-prompt]` | ask first | Starts an agent in a new worktree, idle until given an instruction (`--prompt`: start it on the sentence; `--no-prompt` overrides `new.send_sentence = true`). `--dry-run` is read-only. |
| `fleet watch [--once] [--dry-run]` | ask first | The server itself; normally run by the unit. |

## ship

`ship pr <ref>...` (read, about a second) before any claim about a PR. `ship [<pr>] [--json]`
(read): the state and `next:`. `ship --act --wait` (write): does the mechanical step and waits
through CI, reviews and deploys. `ship findings [<pr>]` lists the craft review's findings;
`ship findings <id>... fixed|dismissed "<why>"` records verdicts. `ship guide <topic>` explains
a step. `ship review [--dry-run]`, `ship rescue`, `ship policy [<repo>]`.

## dev

Read: `dev ls`, `dev repos [--json]`. In your own task: `dev pr-checkout <n> [--repo a]`.
Ask first: `dev <repo> <task>`, `dev cross <task> <repo>...`. Human: `dev done`, `dev prune`.
Never `gh pr checkout` in a canonical clone.

## orch (the `orchestrate` skill)

Read: `orch status [--json]`, `orch report <n>`, `orch log <n> [lines]`, `orch retro`.
Ask first: `orch start`, `orch prompt`. `orch wait` blocks; always pass `--timeout`.
`orch done` only for workers you started.
