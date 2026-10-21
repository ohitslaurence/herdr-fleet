# Keys

File: `~/.config/fleet/env`, lines `NAME=value`, readable only by the user (the tools refuse a
file others can read and carry on without keys). Keys are never put in the environment, so
agents and installs started by the fleet don't inherit them.

| Key | Where | Unlocks |
|---|---|---|
| `TYPESAFE_API_KEY` | console.typesafe.ai → API keys | `fleet new` picks repo, name, agent and model and spots duplicate sessions in one ~0.4 s call; decision points (`watch.offer`, `cockpit.urgency`) when set to `shadow` or `act`; `fleet why` |
| `OPENAI_API_KEY` | platform.openai.com/api-keys (needs Decisions API access) | `fleet new` picks the repo (~150 ms); the name comes from rules |

Neither: `fleet new` reads the sentence with `claude -p --model haiku` when Claude Code is
logged in, else by rules (the repo must be named in the sentence), and says which it used.
Decision points stay off. Nothing else needs a key.

`fleet new` tries Jev, then OpenAI, then Haiku, then rules; a failing key prints one line and
moves on, it never blocks a session.

The user sets one with `fleet keys set <NAME>` (hidden prompt) or removes it with
`fleet keys unset <NAME>`, in their own terminal. You check with `fleet keys status [--json]`
(names, presence, file mode) and `fleet keys test [NAME] [--json]` (one tiny request per key).
