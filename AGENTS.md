# Working on herdr-fleet

Instructions for coding agents changing this repo. The craft review reads this file; the
Learned section came from `fleet learn` over findings agents kept fixing.

## Conventions

- Python 3.11+ standard library only. CLIs live in `bin/` without a `.py` suffix; shared
  helpers are `bin/fleet_*.py`. Tests: `python3 -m unittest discover -s tests`.
- Every command prints help on `-h`/`--help`/`help` anywhere in argv and does nothing else.
- Every config key is in `bin/fleet_config.py` DEFAULTS and documented in
  `docs/configuration.md`; settings read by `fleet watch` live under `[watch]`, `[ship]` keys
  are per repo and read through `ship_policy`.
- Comments say why, not what changed. No compatibility shims for code that never shipped.

## Learned (fleet learn, 2026-10-19)

- When a change alters something README.md, `skills/**/*.md` or a brief states (required
  keys, which PRs a step runs on, numeric caps), update that prose in the same commit. A number
  stated in prose is pinned by a test against the constant it describes.
- Before opening a PR, check every file group in `git diff --stat` against the description:
  each behaviour change gets its own line, including skill loop or `commit_hint` changes, new
  JSON/state keys and changed defaults. Two concerns: split the PR or give each a section.
- Every herdr CLI call goes through a `fleet_proc` helper with a timeout that turns every
  failure (timeout, non-zero exit, unparseable output) into `HerdrDown`, chaining the cause.
- Mutual exclusion between fleet processes uses an flock (`fleet_state.locked`, or
  `fcntl.flock` for try-locks), never a hand-made O_EXCL file: a killed process leaves it behind.

## Intentional (don't flag)

- Private helpers in `bin/ship` with one or two callers in the same file stay light:
  positional tuples rather than NamedTuples, token-shape parsing in `cmd_findings` rather than
  argparse subparsers. Revisit when a helper gains callers outside the file.
