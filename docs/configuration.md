# Configuration

Everything herdr-fleet decides that someone could reasonably want differently is a setting.
Settings live in `~/.config/fleet/config.toml`; anything you leave out uses the default below.
`fleet config show [key] [--repo owner/name]` prints every value and the layer that set it;
`fleet config get <section.key>` prints one bare value for shell tools.

Layers, later wins:

1. **default**: built in, safe for someone who hasn't chosen anything.
2. **preset**: a team file, `preset = "<path>"` in your config.toml. `[lock] keys = [...]` in the
   preset can't be changed by your config.
3. **personal**: your `~/.config/fleet/config.toml`.
4. **repo**: a repo's `.fleet.toml` on its base branch, read by ship per PR. It can only make ship
   more careful: add protected paths, `tier = "critical"`, `merge = "ask"`, turn on
   `require_selfreview` / `cross_review` / `review` / `deploy` / `checks = "all"`, add
   `finding_bots`. Anything else in
   it is ignored and reported.

No environment variable changes policy. `protect.paths` and `watch.human_words` only grow across
layers. Per-repo ship overrides go in `[repos.<name>]` with the same keys as `[ship]`.

## You and your org

| Key | Default | What it does |
|---|---|---|
| `user.name` | `"the user"` | How model prompts refer to you. |
| `user.github` | gh's login | Whose PR comments count as your intent. |
| `github.orgs` | `[]` | Owner for a bare `repo#n`; scope of PR searches. Empty: PRs involving you. |
| `hints.<repo>` | none | One line on what a repo holds, so `fleet new` routes sentences to it. |

## Worktrees and sessions

| Key | Default | What it does |
|---|---|---|
| `worktree.path` | `"{repo_path}-wt-{task}"` | Where `dev` puts a session's worktree. Placeholders: `{repo_path}` `{repo}` `{task}` `{user}`. |
| `worktree.pr_path` | `"{repo_path}-wt-pr-{number}"` | Where a PR checkout goes. |
| `branch.template` | `"{repo}__{task}"` | Branch names, e.g. `"{user}/{task}"`. |
| `dev.repos_dir` | `""` | The folder your git checkouts live in, e.g. `"~/code"`. Each checkout directly inside is a repo named after its folder. |
| `dev.repos.<alias>` | none | `path = "..."` (and optionally `base = "main"`): a repo outside `repos_dir`, a different name, or a different base branch. Wins over the scanned one. |
| `layout.tabs` | `["shell", "agent"]` | Tabs a new session opens with; `agent` runs the agent. |
| `setup.installs` | bun, pnpm, yarn, npm | `[lockfile, command]` pairs; the first lockfile found picks the install command. |
| `spares.repos` | `[]` | Repos that keep one ready worktree, so `fleet new` starts in about a second. Costs a full checkout plus dependencies on disk each. |
| `spares.refresh_minutes` | `20` | How often fleet watch refreshes the spares. |
| `new.classifier` | `"auto"` | How `fleet new` reads a sentence: `auto` (the first with a key: jev, decisions, then haiku), `jev`, `decisions`, `haiku`. `FLEET_CLASSIFIER` overrides once. |
| `new.duplicate_min` | `0.5` | How sure `fleet new` must be that work is already open before offering to jump there. |
| `new.send_sentence` | `false` | Start the agent on your sentence as its first instruction. Off: it waits for you (`p` in the preview, or `--prompt`, sends it once); on: `--no-prompt` or `p` holds it back once. |

## Agents

| Key | Default | What it does |
|---|---|---|
| `agents.permissions` | `"ask"` | `"bypass"`: `fleet new` starts agents with permission checks off (each agent's `bypass` flags). |
| `agents.opposite` | `["claude", "codex"]` | A role with `kind = "opposite"` gets the other of these two from the PR's author. |
| `agents.command_timeout_s` | `90` | A gh/git/herdr call that hangs longer counts as failed. |
| `roles.<role>.kind` / `.model` / `.effort` | driver claude, worker codex, reviewer opposite, advisor claude, security claude | Which harness does what. `roles.driver.kind` is the harness `fleet new` starts unless the sentence names another; `orch` starts from `roles.worker` (or the role `orch start --role` names; `security`: a harness whose filters don't refuse security briefs); the orchestrate skill then picks a harness and model per worker, and `orch start --model <nickname>` uses a `[models.<nickname>]` entry. |
| `orch.quiet_min` | `5` | `orch wait` reports `quiet: idle Nm with no status` for a worker idle this long with no status line (a refused brief, a lost prompt) and returns 2 when every pending worker is quiet. `0` never reports. |
| `kinds.<name>.cmd` | claude, codex, opencode built in | The command that starts the harness. Add `[kinds.<name>]` for any other. |
| `kinds.<name>.bypass` | each harness's skip-permissions flag | Added only with `agents.permissions = "bypass"`. |
| `kinds.<name>.model` / `.model_flag` | none / `"--model"` | Default model when the sentence names none (the driver role's model wins for the driver). |
| `kinds.<name>.system_flag` | claude: `"--append-system-prompt"` | How `fleet new` gives an agent that starts idle the setup note (deps still installing). Without one, that note only reaches agents started on the sentence. |
| `kinds.<name>.about` / `.names` | | Description, and the words that name the harness itself ("in opencode"). |
| `kinds.<name>.models` / `.models_cmd` | claude's aliases / `opencode models` | Models the harness can run: a fixed list, and/or a command printing one id per line (cached a day). |
| `models.<nickname>.harness` / `.model` / `.names` | none | Your own names for models: `[models.kimi] harness = "opencode"`, `model = "opencode/kimi-k3"`. "use kimi" then means that harness and model. |

How `fleet new` picks: a harness the sentence names wins; else a nickname's harness; else the
harness of a model the sentence names; else `roles.driver.kind`. The model is the one named (from
the harness's list or your nicknames; no version named means the newest of that family), else the
default. A model the chosen harness doesn't list gets a warning on the confirm screen; `m` picks
another, `a` changes harness, `--agent` and `--model` force either.

## ship: the PR lifecycle

| Key | Default | What it does |
|---|---|---|
| `ship.merge` | `"ask"` | `"auto"`: merge clean PRs off protected paths. Needs GitHub branch protection to mean anything. |
| `ship.admin` | `false` | Allow `gh pr merge --admin` past the required approval. |
| `ship.method` | `"squash"` | Merge method. |
| `ship.review` | `"greptile"` | Review bot ship waits on, or `"none"`. |
| `ship.finding_bots` | `[]` | Other review bots (login substrings, e.g. a security agent) whose unresolved inline findings block a merge like the review bot's. |
| `ship.checks` | `"required"` | Only checks the base branch requires gate (all, when it requires none). `"all"`: every check gates. |
| `ship.deploy` | `"actions"` | What to watch after a merge: `actions` or `none`. |
| `ship.after_deploy` | `""` | A shell command `ship --act` runs once (in `~`, up to 10 min) when a PR reaches `deployed`, e.g. reinstall a tool you run from main. The outcome goes in the ledger and isn't retried. Only your config or a preset can set it, never a repo's `.fleet.toml`. |
| `ship.tiers.<tier>.min_score` | normal 4, critical 5 | Review score needed to pass. |
| `ship.tiers.<tier>.max_rereviews` | normal 1, critical 3 | Paid re-reviews before ship hands the PR to you with a stuck report. |
| `ship.tiers.<tier>.fixup_max_lines` | normal 80, critical 15 | Lines changed after a passing review that need no re-review. |
| `ship.tiers.<tier>.free_review_first` | normal false, critical true | A free cross-family review before each paid one. |
| `ship.require_selfreview` | `true` | A re-review request needs a self-review comment on the head. |
| `ship.cross_review` | `false` | A free cross-family review on every PR. |
| `ship.max_free_reviews` | `4` | Free reviews per PR before a human looks. |
| `ship.craft_review` | `false` | Before anyone else reviews a draft PR (with `merge = "auto"`, any PR before it merges, since nobody else reviews it), a fresh read-only reviewer (the other model family when it can run sandboxed here, else Claude) reads it once for craft (architecture, readability, tests, clarity, excess). Findings stay in `~/.local/state/fleet/reviews/`, never on the PR; the agent marks each `fixed` or `dismissed` with `ship findings`. After two failed runs it steps aside and says so. `fleet learn` turns the verdicts into proposed rules, lints and tests. |
| `ship.ci_grace_min` | `3` | A head this new with no checks yet: CI hasn't registered. |
| `ship.required_start_min` | `30` | A required check still absent this long won't start: needs a human. |
| `ship.review_timeout_min` | `45` | A review request with no verdict this long counts as dropped. |
| `ship.review_first_grace_min` | `5` | A new PR this young without a review: the bot is still coming. |
| `ship.deploy_start_min` | `5` | Post-merge runs missing this long after the merge aren't coming. |
| `ship.deploy_timeout_min` | `90` | A deploy run still going this long needs a look. |
| `ship.deploy_history_pushes` | `5` | "Always runs on merge" = ran on each of the last N pushes to the base. |
| `ship.deploy_lookback_hours` | `72` | `ship pr` doesn't ask about deploys of older merges. |
| `ship.split_pr_files` | `15` | The stuck report suggests splitting PRs bigger than this. |
| `ship.large_pr_files` | `40` | ...and calls them too big to converge past this. |
| `protect.paths` | `[]` | Globs that make a PR critical and keep merges with a human. Only grows. |
| `repos.<name>.*` | | Any `ship.*` key for one repo, plus `protect_paths`. |

## fleet watch and the cockpit

| Key | Default | What it does |
|---|---|---|
| `watch.interval` | `120` | Seconds between polls (herdr events wake it sooner). |
| `watch.auto_reply` | `false` | Answer safe "want me to...?" offers in worktree panes. Off: they go to the inbox. |
| `watch.auto_reply_text` | built-in wording | What an auto-reply says. |
| `watch.human_words` | `[]` | Extra words that keep an offer with you (vendors, systems). Adds to the built-in list. |
| `watch.blocked_minutes` | `10` | A pane blocked this long goes to the inbox. |
| `watch.merge_lookback_hours` | `6` | On first sight, a PR merged this recently still gets announced. |
| `watch.refresh_minutes` | `10` | How often ship re-checks open PRs for the cockpit. |
| `watch.ntfy_topic` | `""` | Push inbox items to your phone via ntfy.sh (public topics: titles only). |
| `watch.event_settle_s` | `3` | After an agent event, wait this long for its neighbours. |
| `watch.event_min_gap_s` | `15` | Event-woken passes at most this often. |
| `watch.inbox_max_open` / `inbox_open_days` / `inbox_keep_days` / `inbox_max_items` | 100 / 3 / 7 / 500 | Inbox limits. |
| `watch.craft_keep_days` | `14` | After this, a craft review keeps only each finding's summary, rule idea and verdict (what learning needs), not its long text. |
| `cockpit.stale_days` | `7` | Open PRs with no agent and no push this long go to BACKLOG. |
| `cockpit.urgency_ttl_hours` | `6` | How long an urgency score is reused for an unchanged row. |
| `cockpit.recheck_minutes` | `60` | Re-run ship on an unchanged PR at least this often. |

## Decision layer

| Key | Default | What it does |
|---|---|---|
| `decisions.default` | `"off"` | Mode for decision points you haven't set: `off` (no model call), `shadow` (log only), `act`. |
| `decisions."<point>"` | | Per point, e.g. `"watch.offer" = "shadow"`, `"cockpit.urgency" = "act"`. |
| `decisions.model` | `"jev-latest"` | The TypeSafe model `fleet new` and decision points ask. |
| `decisions.debug` | `false` | Keep a redacted tail of the state in the decision log. |

## Remote access

Read-only fleet access for an agent on your tailnet: [remote.md](remote.md).

| Key | Default | What it does |
|---|---|---|
| `remote.port` | `7412` | The port `fleet remote serve` listens on, on this machine's Tailscale address only. |
| `remote.rate_per_min` | `30` | Calls per minute per Tailscale tag, shared by every node with that tag (an ephemeral bot can't reset it by restarting). |
| `remote.scopes.<tag>` | none | `"tag:fleet-client" = ["fleet_status", "fleet_overview", "fleet_inbox"]`: which tools a Tailscale tag may call. Empty refuses everyone; only tags, never people or untagged nodes. |

## Deliberately not configurable

These are security limits or contracts other tools rely on. Changing them would weaken a guard
or break agents written against them, so they're fixed in code.

- **The built-in veto words** (merge, deploy, prod, money, credentials, ...). `watch.human_words`
  adds to them; nothing removes them.
- **How far back the veto reads** (400 lines): an agent can't scroll a dangerous question out of view.
- **Human-only actions**: merge, close, `dev done`, config changes are never exposed to agents or the socket.
- **Socket limits**: owner-only, 10 s request timeout, 64 KiB requests, 32 in flight.
- **Remote API limits and checks**: 16 KiB bodies, a 10 s deadline per request, 16 connections,
  60 requests a minute per source before identification, 300-character text fields, audit
  refusals capped and the log rotated; Tailscale address only;
  whois identity, fail closed; tags only; no browser Origin; the `Fleet-Remote: 1` header.
- **The free reviewer's sandbox**: read-only tools and a minimal environment.
- **ship's states and `--json` fields**: agents and skills are written against them.
- **Where state lives** (`~/.local/state/fleet`): ship's review budget is counted there.
