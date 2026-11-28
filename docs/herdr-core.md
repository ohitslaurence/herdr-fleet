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
| `fleet.session` | The harness session `fleet migrate` resumed there |

The Herdr app shows only terminals that carry its own `herdr.space` label (a space id), each in
a tab of that space. Fleet sets it on every terminal it starts: `[mux] space` if you set it,
else the space of the terminal you ran the command from, else the space most terminals are in.
To find a space's id, read the label off a terminal in it: `hc get <t> --json`. Before the app
has made any space there's nothing to join, and fleet's terminals stay hidden until you label
them (`hc label <t> herdr.space=<id>`).

## What each tool does there

| Tool | On herdr-core |
|---|---|
| `orch start` | A worktree with dev's naming, and the worker in its own terminal named after it, labelled with its group and its orchestrator (`fleet.by`). No `--tab`/`--workspace`. The worker's meta records `mux=core`, so later `orch` commands drive it on core whatever the config says by then. Workers started before backends existed (no `mux=`) stay on herdr after you switch. |
| `dev <repo> <task>` | The worktree, plus a shell terminal labelled with it. Prints the path instead of focusing (the app shows it). `dev done` / `dev prune` end the context's terminals and remove the worktree. |
| `fleet new` | dev's context; setup runs in its shell terminal; the agent gets a terminal of its own named after the session. |
| `fleet watch` | Wakes on `hc events` instead of herdr's socket; reads and prompts agents with `hc read` / `hc send`. |
| `ship`, `fleet note`, `fleet gripe` | Know their own terminal from `HC_TERMINAL_ID`. |
| `fleet ls` | Rows named from terminal names and `fleet.task`. Focus and rename are the app's job (rename sets `fleet.task`). |
| `fleet doctor` | Checks `hc` is up, whether each configured agent harness reports status, and whether new terminals would inherit an agent's session (below). |

## The environment terminals get

An hc terminal gets the environment of the hc *holder* process that keeps it, and a holder keeps
the environment of whatever started it. Start hc from inside an agent (any `hc` command starts
the server) and every terminal inherits that agent's variables: its herdr pane, which an agent
there would report and act as, and its Claude Code session markers. A Claude Code started under
another's `CLAUDE_CODE_CHILD_SESSION` runs as its child and **saves no transcript**.

Fleet unsets these in every program it starts on core (`env -u ...`, the list is
`fleet_mux.CALLER_ENV`), so its own agents are fine either way. Shells you open from the Herdr app
are not: `fleet doctor` warns (`core.env`) when the current holder has them. To fix it, from a
plain login shell (not inside an agent) with nothing open on core: kill the `herdr-core hold`
processes, run `hc server restart`, then open one terminal so the new holder starts from that shell.

## Moving running agents over: `fleet migrate`

A running program can't change multiplexer, but its session can. `fleet migrate` starts each
herdr agent again on core in the same directory, resuming its session (`claude --resume <id>`,
`codex resume <id>`) with the flags it was started with, under its herdr name. The conversation,
worktree, branch and uncommitted files carry over; the process and its prompt cache are new.

```
fleet server stop
fleet migrate --dry-run --all     # what would move, the command each would run, and who stays
fleet migrate --all               # asks first; --yes to skip
# then [mux] backend = "core" and: fleet server start
```

- Only an agent that is idle or done moves, and it's checked again just before it moves.
- Its herdr pane closes only once the new terminal has stayed up, so a resume that fails leaves it where it was.
- Fleet's records follow it: orch worker meta (`pane=`, `mux=core`, and `by=` of its workers),
  ship ledgers, notes and the watcher's inbox.
- `--all` leaves out harnesses that don't report status on core (Codex before 0.162.2) and ones
  fleet can't resume (opencode); name a pane or agent to move it anyway. The pane running
  `fleet migrate` stays: run it from another.
- Stop the watcher first: it keeps its inbox in memory and would write the old panes back.

To try core before committing, `fleet migrate --keep` starts the copies and changes nothing else:
the herdr panes stay open and fleet's records still point at them. Both sides resume the same
session, so type into one at a time, and keep the watcher stopped meanwhile (it would prompt
both). Happy? Run `fleet migrate` on the same agents without `--keep`: it finds each copy by its
`fleet.session` label, moves the records to it and closes the herdr pane. Not happy? Close the
copies (`hc kill`) and carry on in herdr.

## Setup

1. Install herdr-core on the machine that runs the agents: `curl -fsSL https://releases.herdr.dev/core/install.sh | sh`.
2. On the machine you sit at, install the Herdr app and add the agents' machine: `hc machine add <host>`.
3. Set `backend = "core"` as above. `fleet mux agents` lists what fleet sees.
