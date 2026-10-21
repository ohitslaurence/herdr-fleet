"""fleet_manifest — what install.sh and `fleet skills install` created, so uninstall removes
exactly that, in ~/.local/state/fleet/install.json:
  {"links": [...], "plugin": bool, "skills": [...],
   "managed_by": "install.sh" | "herdr"}   (herdr: `herdr plugin install` ran it; updates go through herdr)"""
import json, os

import fleet_config

PATH = os.path.join(fleet_config.STATE_DIR, "install.json")


def read():
    try:
        with open(PATH) as f:
            m = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        m = {}
    m.setdefault("links", [])
    m.setdefault("plugin", False)
    m.setdefault("skills", [])
    m.setdefault("managed_by", "install.sh")
    return m


def write(m):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH, "w") as f:
        json.dump(m, f, indent=1)


def update(fn):
    m = read()
    fn(m)
    write(m)
