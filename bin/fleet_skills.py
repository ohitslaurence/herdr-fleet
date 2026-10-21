"""fleet_skills — install the fleet skills into each agent harness, as stamped copies.

  fleet skills status [--json]          which harnesses have them, at which version
  fleet skills install [--to a,b] [--yes]   copy skills/fleet and skills/orchestrate into each
                                            harness's skills folder

`fleet` is the one every agent needs; `orchestrate` is for an agent driving workers with orch.

A copy, never a symlink into this checkout: what an agent follows changes only when you
install or update, not when someone edits the checkout. The copy's SKILL.md carries this
install's version (plus commit), so `fleet doctor` spots a stale one.
"""
import json, os, shutil, sys, tempfile

import fleet_manifest
import fleet_version

SKILLS = ("fleet", "orchestrate")  # folders under skills/, installed side by side in each harness
HARNESSES = {  # name: (the harness's own dir, which must exist, and its skills folder)
    "claude": ("~/.claude", "~/.claude/skills"),
    "codex": ("~/.codex", "~/.codex/skills"),
    "agents": ("~/.agents", "~/.agents/skills"),
    "opencode": ("~/.config/opencode", "~/.config/opencode/skills"),
}


def stamp():
    i = fleet_version.info()
    return i["version"] + (f"+{i['commit']}" if i["commit"] else "")


def frontmatter(text):
    """{key: value} from the block between the first two `---` lines (flat, plus metadata.version)."""
    if not text.startswith("---\n"):
        return {}
    block = text[4:].split("\n---", 1)[0]
    out = {}
    for line in block.splitlines():
        key, _, value = line.strip().partition(":")
        if value.strip():
            out[key.strip()] = value.strip().strip('"')
    return out


def installed_version(skill_dir):
    """The version stamped in our copy of the skill this folder is named for; None when it isn't one."""
    name = os.path.basename(skill_dir)
    try:
        with open(os.path.join(skill_dir, "SKILL.md")) as f:
            fm = frontmatter(f.read())
    except OSError:
        return None
    return fm.get("version") if name in SKILLS and fm.get("name") == name else None


def targets():
    """{harness: skills dir} for harnesses present on this machine."""
    return {h: os.path.expanduser(d) for h, (home, d) in HARNESSES.items() if os.path.isdir(os.path.expanduser(home))}


def status():
    """Per harness: each skill's copy, and whether all of them are current. The top-level
    installed/path are the fleet skill's (the one every agent needs)."""
    want = stamp()
    out = {}
    for h, d in targets().items():
        skills = {}
        for name in SKILLS:
            dest = os.path.join(d, name)
            have = installed_version(dest)
            skills[name] = {"path": dest, "installed": have, "current": have == want and not os.path.islink(dest),
                            "symlink": os.path.islink(dest)}  # an old install linked into a checkout
        out[h] = {"path": skills["fleet"]["path"], "installed": skills["fleet"]["installed"],
                  "current": all(x["current"] for x in skills.values()),
                  "symlink": any(x["symlink"] for x in skills.values()), "skills": skills}
    return {"version": want, "harnesses": out}


def copy_to(name, dest):
    """Replace dest with a stamped copy of skills/<name>, atomically per skill dir."""
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".fleet-skill-", dir=parent)
    staged = os.path.join(tmp, name)
    shutil.copytree(os.path.join(fleet_version.ROOT, "skills", name), staged)
    path = os.path.join(staged, "SKILL.md")
    with open(path) as f:
        text = f.read().replace("{{FLEET_VERSION}}", stamp())
    with open(path, "w") as f:
        f.write(text)
    if os.path.islink(dest) or os.path.isfile(dest):
        os.remove(dest)
    elif os.path.isdir(dest):
        shutil.rmtree(dest)
    os.rename(staged, dest)
    shutil.rmtree(tmp, ignore_errors=True)


def install(to, assume_yes):
    """Install into `to` (harness names). Refuses a destination that isn't ours."""
    found = targets()
    unknown = [h for h in to if h not in HARNESSES]
    if unknown:
        print(f"fleet skills: unknown harness {', '.join(unknown)} (have: {', '.join(HARNESSES)})", file=sys.stderr)
        return 2
    plan, refused = [], []
    for h in to:
        if h not in found:
            refused.append(f"{h}: {HARNESSES[h][0]} doesn't exist (harness not installed)")
            continue
        for name in SKILLS:
            dest = os.path.join(found[h], name)
            if os.path.exists(dest) and not os.path.islink(dest) and installed_version(dest) is None:
                refused.append(f"{h}: {dest} exists and isn't our {name} skill: left alone")
                continue
            plan.append((h, name, dest))
    for line in refused:
        print(f"skip  {line}")
    for h, name, dest in plan:
        print(f"copy  skills/{name} -> {dest}  (version {stamp()})")
    if not plan:
        return 1 if refused else 0
    import fleet_term
    if not fleet_term.confirm("fleet skills", "go ahead?", assume_yes):
        print("nothing changed")
        return 3
    for h, name, dest in plan:
        copy_to(name, dest)
    fleet_manifest.update(lambda m: m["skills"].extend(d for _, _, d in plan if d not in m["skills"]))
    print("done. Agents load them next session; `fleet doctor --only skill` checks them.")
    return 0


def refresh():
    """After an update: re-copy every skill install recorded, so agents follow the new release."""
    recorded = fleet_manifest.read()["skills"]
    for dest in recorded:
        if installed_version(dest) is not None:
            copy_to(os.path.basename(dest), dest)
    # Installs from v0.3.0 and earlier have no orchestrate skill (the fleet skill held it): add it
    # beside each fleet copy, over a link left pointing at the old folder. Delete with v0.3 installs.
    added = []
    for dest in recorded:
        sibling = os.path.join(os.path.dirname(dest), "orchestrate")
        if os.path.basename(dest) != "fleet" or sibling in recorded or installed_version(dest) is None:
            continue
        if os.path.islink(sibling) or not os.path.exists(sibling):
            copy_to("orchestrate", sibling)
            added.append(sibling)
    if added:
        fleet_manifest.update(lambda m: m["skills"].extend(d for d in added if d not in m["skills"]))


def main(argv):
    cmd, rest = (argv[0], argv[1:]) if argv else ("status", [])
    if cmd == "status":
        st = status()
        if "--json" in rest:
            print(json.dumps(st, indent=1))
            return 0
        print(f"fleet skills {st['version']}")
        for h, x in st["harnesses"].items():
            for name, s in x["skills"].items():
                state = ("current" if s["current"] else f"stale ({s['installed']})" if s["installed"]
                         else "not installed")
                print(f"  {h:9} {name:12} {state:22} {s['path']}")
        return 0
    if cmd == "install":
        to, yes = list(targets()), False
        while rest:
            a = rest.pop(0)
            if a == "--yes":
                yes = True
            elif a == "--to" and rest and not rest[0].startswith("-"):
                to = rest.pop(0).split(",")
            else:  # a missing --to value must never widen the install to every harness
                print(f"fleet skills install: bad argument {a!r}; usage: [--to claude,codex,agents,opencode] [--yes]",
                      file=sys.stderr)
                return 2
        return install(to, yes)
    print(f"fleet skills: unknown command {cmd!r}; see fleet skills --help", file=sys.stderr)
    return 2
