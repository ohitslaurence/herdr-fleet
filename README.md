# herdr-fleet

**Run twenty coding agents like a team, not twenty terminals.**

herdr-fleet turns [herdr](https://herdr.dev) into a control room for Claude Code, Codex,
opencode and any other agent CLI. You say what you want in one sentence; an agent starts in its
own worktree. It opens a PR; `ship` drives that PR through review, CI, merge and deploy one step
at a time. Everything that actually needs you lands on one screen. The rest happens without you.

> Status: **pre-1.0** (0.4): used daily by its author, still changing; expect breaking changes
> between minor versions, each listed in the [changelog](CHANGELOG.md). Linux and macOS
> (macOS is tested in CI but sees less daily use; it needs Homebrew's `python` and `bash`).
> Standard-library Python only: nothing to `pip install`, no dependency supply chain.
> For people running several agent CLIs at once in herdr, on GitHub repos. Not for GitLab or
> Windows (yet). herdr-fleet is an independent plugin: it isn't made or endorsed by herdr's authors.

## What it feels like

```
$ fleet new "api, fix the flaky login test"
  repo   api       (named in the sentence)
  name   api-fix-flaky-login-test
  agent  claude · model opus, waiting for your first instruction
  setup  spare worktree ready
```

Seconds later an agent is waiting in `~/code/api-wt-fix-flaky-login-test`, on its own branch,
in its own herdr tab, dependencies installing, and the cockpit already knows what the session is
for. You give it its first instruction (or press `p` and the sentence is it). When it's done it
opens a draft PR and runs
`ship --act --wait`, which tells it exactly one thing to do next until the PR lands:

```
acme/api#412  craft-findings   3 craft finding(s) need a verdict: `ship findings` lists them ...
acme/api#412  ci-running       wait: `ship --wait`
acme/api#412  review-findings  2 open P1 finding(s), Greptile 3/5: work them per `ship guide findings` ...
acme/api#412  awaiting-human   clean (CI green, review passed); a human approves/merges
```

Meanwhile you open the cockpit (`ctrl+w f` in herdr, or just `fleet`) and see only what needs you:

```
fleet  ·  PR data 2m old

NEEDS YOU (2)
  PR api#398            deploy-failed    ‼ Retry stuck refunds · investigate the failed post-merge run
  ●  web-wt-onboarding  decision         "Should I also migrate the old sessions table?"

READY TO MERGE (3)
  PR api#412            awaiting-human   Fix the flaky login test
  PR web#77             awaiting-human   ‼ Enforce the fee-inclusive deposit minimum  [critical]
  ...
IN FLIGHT (11)
```

Enter jumps to the agent's pane, `o` opens the PR, `m` merges after asking. You merge; the
agents do the rest.

## What you get

**Sessions from one sentence.** `fleet new` reads "api, fix the flaky login test" (or "web and
api, add passkeys, use codex") and picks the repo, a branch name, the agent and the model. It
notices when an open session is already doing the same work and offers to jump there instead.
Spare worktrees, kept ready per repo, make a start almost instant. Your names for models ("use kimi") are yours
to define.

**`ship`: one PR, one next action.** A state machine over everything GitHub knows about a PR:
CI (only the checks the base branch requires), review bots, budgets for paid re-reviews,
conflicts, merge policy and the deploy after the merge. Agents run `ship --act --wait` in a loop
and do what `next:` says. `ship pr <ref>` answers "is it merged?" straight from GitHub, so
nobody guesses. Paid re-reviews are earned: a self-review first, a free review by the other model
family, and a fresh-agent rescue when fixes stop converging.

**A quiet craft review that compounds** (opt-in: `ship.craft_review = true`). Before anyone else
sees a draft (and before an auto-merge, ready or not), a fresh reviewer reads it once for architecture, readability, tests, clarity and
excess. Findings stay on your machine,
never as PR comments. The agent marks each one fixed or dismissed, with a reason. `fleet learn`
reads those verdicts across PRs and proposes the lint rule, test or `AGENTS.md` line that would
have prevented the ones that keep coming back.

**A watcher that knows what matters.** `fleet watch` runs as a service and wakes on herdr's agent
events: merges, deploys, agents stuck on a question, agents offering to do something, workers
finished while their orchestrator sat idle. Anything
touching merges, deploys, money, credentials or deletions always comes to your inbox; harmless
offers can be answered for you, if you turn that on.

**Workers.** `orch` fans a task out to several agents in their own worktrees, briefs them from a
template, watches an append-only status log, and stops on any dialog it can't vouch for.
`orch wait` hands each worker's result back as soon as that worker finishes.

**It checks itself.** `fleet doctor` checks the whole install at once (herdr and
its plugin, your agents and their logins, gh, repos, config, keys, the watcher, the skill) and
gives each problem its exact fix.

## Install

Needs [herdr](https://herdr.dev) 0.9+, Python 3.11+, git and the GitHub CLI (`gh auth login`).
On macOS, Apple's own `python3` (3.9) and `bash` (3.2) are too old: `brew install python bash`
and keep Homebrew's bin ahead of `/usr/bin` on PATH (`fleet doctor` checks both).

```sh
git clone --branch v0.4.0 https://github.com/ohitslaurence/herdr-fleet.git ~/.local/share/herdr-fleet
~/.local/share/herdr-fleet/install.sh   # shows every change, then links commands and the plugin
```

Then, each of which shows what it will do before doing it:

```sh
fleet setup             # propose a config from this machine (repos, orgs, agents), then write it
fleet server install    # keep the watcher running across reboots (systemd or launchd)*
fleet skills install    # teach Claude Code, Codex and opencode how to use the fleet
fleet keys set OPENAI_API_KEY     # optional, or TYPESAFE_API_KEY: smarter `fleet new` (Keys below)
fleet doctor            # everything green? each problem comes with its fix
```

\* The unit runs this install's absolute path with the PATH of your login shell, so keep the
install where it is (or run `fleet server install` again after moving it).

Or let herdr do the cloning: `herdr plugin install ohitslaurence/herdr-fleet --ref v0.4.0` shows the
plugin and the install command it runs, then installs both (`herdr plugin update herdr-fleet`
updates it).

`fleet doctor --only herdr.keys` prints the herdr keybindings for `ctrl+w f` (cockpit) and
`ctrl+w t` (new session). `fleet update` moves to a newer release after showing the changelog
and every change to skills and the installer; `fleet uninstall` removes what `install.sh` added
and leaves your config, keys and state.

**Or ask your agent.** With the skill installed, "make sure the fleet is going" runs doctor,
fixes what an agent may fix, and tells you the exact commands for the rest.

## Safe by default

herdr-fleet ships with the cautious choice everywhere, and each one is a setting you can change:

- **You merge.** `ship` stops at `awaiting-human` unless you set `merge = "auto"` for a repo
  (sensible only with branch protection and required review).
- **Agents ask before tools.** `fleet new` starts agents with their permission prompts on unless
  you choose `agents.permissions = "bypass"`. Unattended `orch` workers are the exception;
  [SECURITY.md](SECURITY.md) says why and how they're contained.
- **No auto-replies, and no decision point calls a model,** until you turn them on.
- **Human-only actions stay human.** Merging, closing PRs, `dev done`, config and key changes
  are never exposed to agents or the local socket; config and key writes need your terminal.
- **Policy only gets stricter from below.** A repo's `.fleet.toml` can add protected paths or
  require review, never loosen yours. No environment variable changes policy.
- **Keys** live in an owner-only file, never in the environment agents inherit, and no command
  prints them.

What the tools trust and refuse to decide for you, including the honest limits (all agents run
as one unix user, so local gates are guards, not boundaries): [SECURITY.md](SECURITY.md).

## Commands

| Command | What |
|---|---|
| `fleet` / `fleet ls` | the cockpit: what needs you, what's ready, what's in flight |
| `fleet new "<repo>, <what>"` | start a session from one sentence |
| `fleet inbox` | agents' questions and offers waiting for you |
| `ship [<pr>] [--act --wait]` | a PR's state and its one next action |
| `ship pr <ref>` | is it open, merged, deployed? |
| `ship findings` | the craft review's findings and their verdicts |
| `dev <repo> <task>` | a worktree session by hand |
| `orch start` · `wait` · `report` | supervise worker agents |
| `fleet doctor` · `setup` · `keys` · `server` · `skills` | install health and setup |
| `fleet learn [--propose]` | turn review verdicts into rules |
| `fleet config show [key]` | every setting and where it came from |
| `fleet update` · `uninstall` · `version` | lifecycle |

Every command prints help on `--help` (anywhere in its arguments) and does nothing else, so
agents can explore safely. `--json` wherever a program might read the answer.

## For agents

`skills/fleet` is an [Agent Skills](https://agentskills.io) skill: the rules first (what an agent
must never do), then the doctor loop, setup, keys, the ship loop, and every command by kind:
read, write, ask first, human only. `fleet skills install` puts a version-stamped copy into each
agent harness on the machine, and `fleet doctor` notices a stale one. `ship guide <topic>` serves
the detailed steps from the
copy installed with the tools (`ship guide` alone lists the topics), so the instructions always
match the binary.

## Configuration

Settings come from layers, later wins: built-in defaults, an optional team preset (whose
`[lock]` keys can't be overridden), your `~/.config/fleet/config.toml`, and a repo's
`.fleet.toml` on its base branch, which can only tighten. `fleet config show` prints every value
and its layer; `fleet doctor` flags typos (`agnets`: did you mean `agents`?) and invalid choices.
Every setting, its default and what it does: [docs/configuration.md](docs/configuration.md).

A personal config might look like:

```toml
[user]
name   = "Ada"                    # how model prompts refer to you (default "the user")
github = "ada"                    # whose PR comments count as your intent (default: gh's login)

[github]
orgs = ["acme"]                   # owner for bare `repo#n`, scope for PR searches (default: none)

[dev]
repos_dir = "~/code"              # your git checkouts; each folder inside is a repo
warm_caches = [".cache/typecheck"]  # build caches a new worktree copies from the newest sibling

[dev.repos.infra]                 # one that lives elsewhere, or a different base branch
path = "~/work/infrastructure"
base = "develop"

[agents]
permissions = "ask"               # "bypass": fleet new starts agents with permission checks off

[roles.driver]
kind = "claude"                   # the agent fleet new starts unless the sentence names another

[models.kimi]                     # your name for a model: "use kimi" picks this harness + model
harness = "opencode"              # harnesses built in: claude, codex, opencode ([kinds.<name>] adds more)
model = "opencode/kimi-k3"

[ship]
review = "greptile"               # review bot ship waits on (bin/fleet_review.py), or "none"
craft_review = true               # the quiet craft review on every draft (every PR with merge = "auto")

[watch]
human_words = ["stripe", "payroll\\w*"]  # extra words that keep an agent's offer with you;
                                         # they add to the built-in list, never replace it

[worktree]
path    = "{repo_path}-wt-{task}" # also "{repo_path}/.worktrees/{task}", "~/wt/{repo}/{task}"
pr_path = "{repo_path}-wt-pr-{number}"

[branch]
template = "{repo}__{task}"       # also "{user}/{task}"
```

## Keys

Optional: nothing needs a key. With one, `fleet new` reads your sentence with a model instead of
rules. Any of these works, and `new.classifier` picks (the default uses the first you have):

- `TYPESAFE_API_KEY` ([TypeSafe](https://console.typesafe.ai/)): repo, name, agent and model in
  one call; also enables decision points.
- `OPENAI_API_KEY` ([OpenAI](https://platform.openai.com/api-keys), needs Decisions API access).
- No key: Claude Haiku through the `claude` CLI if you have it, else plain rules. `fleet new`
  says which it used.

herdr-fleet has no affiliation with either provider.

```sh
fleet keys set OPENAI_API_KEY     # hidden prompt; needs your terminal
fleet keys status                 # names and presence, never values
fleet keys test                   # one tiny request per key
```

Keys live in `~/.config/fleet/env`, readable only by you (a file others can read is refused).

## What's here

| Path | What |
|---|---|
| `bin/fleet`, `bin/fleet-cockpit` | the `fleet` front door and the cockpit |
| `bin/fleet-new` | sessions from one sentence |
| `bin/fleet-watch`, `bin/fleet_serve.py` | the watcher and its owner-only local socket |
| `bin/ship` | the PR state machine |
| `bin/dev`, `bin/orch` | worktree sessions; worker supervision |
| `bin/fleet_*.py` | config, doctor, setup, keys, server, install, skills, learn, decisions |
| `install.sh` | the installer (`bin/fleet_install.py` also does update and uninstall) |
| `skills/` | the `fleet` and `orchestrate` skills, plus the guides `ship guide` serves |
| `herdr-plugin.toml` | the herdr plugin (cockpit and new-session popups) |
| `docs/configuration.md`, `SECURITY.md`, `CHANGELOG.md`, `AGENTS.md` | reference |
| `tests/` | `python3 -m unittest discover -s tests` (golden tests on anonymised real PRs) |

Everything runs from `bin/` with no build step: the commands and the `fleet_*.py` modules they
import sit side by side (each command puts its own directory on `sys.path`). You start with
`fleet`; `ship`, `orch` and `dev` are mostly what your agents run.

## Licence

MIT, see [LICENSE](LICENSE).
