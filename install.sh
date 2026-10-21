#!/bin/sh
# herdr-fleet installer: links this checkout's commands into ~/.local/bin and its plugin into
# herdr, after showing exactly what it will do. Recommended: clone a release, then run this:
#   git clone --branch v0.4.0 https://github.com/ohitslaurence/herdr-fleet.git ~/.local/share/herdr-fleet
#   ~/.local/share/herdr-fleet/install.sh
# Undo with `fleet uninstall`. Pass --yes to skip the confirmation.
set -eu
here=$(cd "$(dirname "$0")" && pwd -P)
for py in python3.13 python3.12 python3.11 python3; do
    if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
        exec "$py" "$here/bin/fleet_install.py" install "$@"
    fi
done
echo "herdr-fleet needs python 3.11 or newer (for tomllib); none found on PATH" >&2
exit 1
