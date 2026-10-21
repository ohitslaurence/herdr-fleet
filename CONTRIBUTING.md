# Contributing

Thanks for looking. herdr-fleet is maintained by one person, who reviews and merges every
change. Issues and pull requests are welcome; please read this first so yours lands quickly.

## Before you start

- **Bugs:** open an issue with the bug template (it asks for `fleet doctor --json`).
- **Anything bigger than a fix:** open an issue with the idea template first, so we agree on the
  shape before you write it. A large PR nobody asked for is likely to sit.
- **Security problems:** never in an issue or PR. See [SECURITY.md](SECURITY.md).

## Working on it

```sh
git clone https://github.com/ohitslaurence/herdr-fleet.git && cd herdr-fleet
python3 -m unittest discover -s tests      # about a minute; needs git, no network
```

Running from a checkout: `./install.sh` links this checkout's commands into `~/.local/bin`
(it shows every change first; `fleet uninstall` undoes it).

The rules, in short. [AGENTS.md](AGENTS.md) has the full list, written for coding agents but
just as true for people:

- Python 3.11+ standard library only. No dependencies, ever: that's a feature.
- Every command prints its help on `-h`, `--help` or `help` anywhere in its arguments, and does
  nothing else.
- Every setting lives in `bin/fleet_config.py` DEFAULTS and in `docs/configuration.md`.
- Nothing personal or organisation-specific in the code: names, paths and policies belong in
  the user's `~/.config/fleet/config.toml`. CI checks this.
- Text from PRs, reviews, CI logs and agents is untrusted. It never becomes a command, a policy,
  or a merge decision.
- Comments say why, not what. Tests check behaviour, not implementation.

## Pull requests

- One concern per PR (or one section per concern in the description).
- New behaviour comes with a test; a changed command updates its `--help`, the docs and the
  `## Unreleased` section of [CHANGELOG.md](CHANGELOG.md).
- CI must pass on Python 3.11 and 3.13, on Linux and macOS.
- Workflow files (`.github/`) and anything security-sensitive get a closer review and may take
  longer.

By contributing you agree your work is released under the [MIT licence](LICENSE).
