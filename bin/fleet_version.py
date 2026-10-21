"""fleet_version — which herdr-fleet this is: the plugin manifest's version, plus the commit when
it runs from a git checkout. doctor compares the running watcher's ping against it."""
import os, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet_py  # noqa: E402,F401  (Python 3.11+, before tomllib)
import tomllib  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
MANIFEST = os.path.join(ROOT, "herdr-plugin.toml")
TESTED_HERDR_MAX = "0.9.5"  # newest herdr a release was tested on; doctor warns past it


def manifest():
    with open(MANIFEST, "rb") as f:
        return tomllib.load(f)


def info():
    """{"version", "commit", "install", "source": "checkout"|"release"}."""
    commit = None
    if os.path.exists(os.path.join(ROOT, ".git")):
        try:
            p = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"], capture_output=True,
                               text=True, timeout=5)
            commit = p.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {"version": manifest()["version"], "commit": commit, "install": ROOT,
            "source": "checkout" if commit else "release"}


def line():
    i = info()
    return f"herdr-fleet {i['version']}" + (f" ({i['commit']})" if i["commit"] else "")
