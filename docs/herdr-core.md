# Running the fleet on herdr-core

herdr-fleet can host its agents in either of two multiplexers:

| `[mux] backend` | What it is | Agent status comes from |
|---|---|---|
| `herdr` (default) | The herdr TUI (`herdr`): workspaces, tabs, panes | herdr reading each agent's screen |
| `core` | herdr-core (`hc`), the runtime the Herdr desktop and phone apps are clients of | the agent itself, over OSC 7501 |

Set it in `~/.config/fleet/config.toml`, or per command with `FLEET_MUX=core`:

```toml
[mux]
backend = "core"
```

The two don't share agents: a session started in one doesn't appear in the other.

## Status on core

herdr-core never guesses status from the screen. A terminal has a status only when its program
reports one through OSC 7501 (the Program Status Protocol):

| Agent | Reports status |
|---|---|
| Claude Code | Yes, from 2.1.295. Not inside tmux, and not with `CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1`. |
| Codex | Builds after 0.162.1. Older Codex runs, but shows `unknown`. |
| opencode | No. Shows `unknown`. |

Fleet maps core's words to the ones it already uses: `working`, `blocked` (core's `blocked` and
`error`), `idle`, `done` (a finished turn nobody has looked at, using the Herdr apps' shared
`herdr.seen` label) and `unknown` (never reported).

## Grouping

Core has no workspaces or tabs; the Herdr app keeps its own layout. Fleet groups its terminals
with labels, which every Herdr client can read:

| Label | Meaning |
|---|---|
| `fleet.group` | The worktree (or other unit) the terminal belongs to |
| `fleet.kind` | The agent harness: `claude`, `codex`, `opencode` |
| `fleet.worktree`, `fleet.task`, `fleet.repo_root` | The dev context: its checkout, its task name, its repo |
| `fleet.by` | An orch worker's orchestrator terminal |

## What each tool does there

| Tool | On herdr-core |
|---|---|
| `orch start` | A worktree with dev's naming, and the worker in its own terminal named after it, labelled with its group and its orchestrator (`fleet.by`). No `--tab`/`--workspace`. The worker's meta records `mux=core`, so later `orch` commands drive it on core whatever the config says by then. Workers started before backends existed (no `mux=`) stay on herdr after you switch. |
| `dev <repo> <task>` | The worktree, plus a shell terminal labelled with it. Prints the path instead of focusing (the app shows it). `dev done` / `dev prune` end the context's terminals and remove the worktree. |
| `fleet new` | dev's context; setup runs in its shell terminal; the agent gets a terminal of its own named after the session. |
| `fleet watch` | Wakes on `hc events` instead of herdr's socket; reads and prompts agents with `hc read` / `hc send`. |
| `ship`, `fleet note`, `fleet gripe` | Know their own terminal from `HC_TERMINAL_ID`. |
| `fleet ls` | Rows named from terminal names and `fleet.task`. Focus and rename are the app's job (rename sets `fleet.task`). |
| `fleet doctor` | Checks `hc` is up and whether each configured agent harness reports status. |

A terminal started by `hc` inherits the environment of whoever started it; fleet drops herdr's
variables (`HERDR_PANE_ID` and friends) from the terminals it starts, so an agent there never
acts as the herdr pane that launched it.

## Setup

1. Install herdr-core on the machine that runs the agents: `curl -fsSL https://releases.herdr.dev/core/install.sh | sh`.
2. On the machine you sit at, install the Herdr app and add the agents' machine: `hc machine add <host>`.
3. Set `backend = "core"` as above. `fleet mux agents` lists what fleet sees.
