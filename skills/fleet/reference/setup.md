# First-time setup: what to propose, and why

`fleet setup --propose --json` does this inspection and returns `proposed`, `evidence`, `ask`
and the TOML it would write. Ask the user about every key in `ask`; keep the default for the
rest and say so in one line.

| Setting | How the value is found | Default / safe value |
|---|---|---|
| `dev.repos_dir` | The folder with the most git checkouts among `~/dev ~/code ~/src ~/projects ~/work ~/git ~/repos`, one level down too, and `ghq root`. **Ask** if several have repos. | none: `dev`, `fleet new` and `orch` need it |
| `dev.repos.<alias>` | Not proposed. For a repo outside `repos_dir` or a different base branch: `path`, `base`. | none |
| `worktree.path` | Existing worktrees inside the repo → `{repo_path}/.worktrees/{task}`. | `{repo_path}-wt-{task}` (beside the repo) |
| `branch.template` | 10+ recent branches start with your user or GitHub login → `{user}/{task}`. | `{repo}__{task}` |
| `user.name`, `user.github` | `git config user.name`; `gh api user`. | "the user"; gh's login |
| `github.orgs` | Owners of `origin` across your repos that you're a member of. **Ask** which are work orgs. | `[]` |
| `roles.driver` | Agent CLIs on PATH (claude, codex, opencode). **Ask** if several. | `claude` |
| `agents.permissions` | **Always ask.** `ask` = agents stop for approval on tools (safe, slower); `bypass` = no checks, every agent runs as the user. | `ask` |
| `spares.repos` | **Ask**: 1-2 busy repos with a lockfile. Each costs a full checkout plus dependencies on disk, and makes `fleet new` start in about a second. | `[]` |
| `ship.merge` | Only if the user raises it. `auto` is safe only with branch protection and required review. | `ask` |
| `watch.auto_reply` | Leave off until the user has seen the inbox for a while. | `false` |
| `decisions.default` | With `TYPESAFE_API_KEY`: offer `shadow` (logs what it would do, changes nothing). | `off` |

`--propose` never proposes `agents.permissions = "bypass"`, `ship.merge = "auto"`,
`watch.auto_reply = true` or `ship.admin = true`: those come only from the human.

With a config already there it only adds what the file doesn't set. Present the result as one
table, then the TOML, then the command for the user: `fleet setup apply <file>`.
