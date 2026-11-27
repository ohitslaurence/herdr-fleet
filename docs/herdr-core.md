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

## Setup

1. Install herdr-core on the machine that runs the agents: `curl -fsSL https://releases.herdr.dev/core/install.sh | sh`.
2. On the machine you sit at, install the Herdr app and add the agents' machine: `hc machine add <host>`.
3. Set `backend = "core"` as above. `fleet mux agents` lists what fleet sees.
