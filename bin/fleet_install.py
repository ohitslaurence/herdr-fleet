"""fleet_install — put this checkout on PATH and in herdr, update it to a release, take it out.

  install.sh [--yes]                  link the commands into ~/.local/bin and the plugin into herdr
  install.sh --yes --from-herdr       the same, run by `herdr plugin install` (herdr owns the plugin
                                      and its updates)
  fleet update [--check] [--json] [--yes]   move this install to the newest release tag, after
                                      showing what changes (changelog, skills, installer) and asking
  fleet uninstall [--yes]             remove exactly what install added; config, keys and state stay

Writes only under your home (no sudo) and prints every change before making it. A manifest
(~/.local/state/fleet/install.json) records what install created, so uninstall removes only
that. Updates move between release tags, never to main: the skills and commands your agents
follow change only when you say yes to a release you've seen.
"""
import json, os, re, shutil, sys

import fleet_config
import fleet_manifest
import fleet_proc
import fleet_term
import fleet_version

ROOT = fleet_version.ROOT
BINS = ("dev", "fleet", "fleet-cockpit", "fleet-new", "fleet-watch", "orch", "ship")
BIN_DIR = os.path.expanduser("~/.local/bin")
TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
WATCHED = ("skills/", "install.sh", "bin/fleet_install.py", "herdr-plugin.toml")  # shown in full on update


def git(*args, check=True):
    p = fleet_proc.run(["git", "-C", ROOT, *args], timeout=120)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])}: {(p.stderr or p.stdout).strip()[:300]}")
    return p.stdout.strip()


def plan_links():
    """[(link, target, action)]: create, keep (already ours), relink (a link elsewhere) or conflict."""
    out = []
    for b in BINS:
        link, target = os.path.join(BIN_DIR, b), os.path.join(ROOT, "bin", b)
        if os.path.islink(link):
            out.append((link, target, "keep" if os.path.realpath(link) == os.path.realpath(target) else "relink"))
        elif os.path.exists(link):
            out.append((link, target, "conflict"))
        else:
            out.append((link, target, "create"))
    return out


def link(quiet=False):
    """Create or repoint the command links; record what we created. Returns the conflicts."""
    m = fleet_manifest.read()
    conflicts = []
    for path, target, action in plan_links():
        if action == "conflict":
            conflicts.append(path)
            continue
        if action != "keep":
            os.makedirs(BIN_DIR, exist_ok=True)
            if os.path.islink(path):
                os.remove(path)
            os.symlink(target, path)
            if not quiet:
                print(f"linked {path} -> {target}")
        if path not in m["links"]:
            m["links"].append(path)
    fleet_manifest.write(m)
    return conflicts


def link_plugin():
    """Link the herdr plugin; None, or why it couldn't."""
    try:
        fleet_proc.herdr("plugin", "unlink", "herdr-fleet")
    except fleet_proc.HerdrDown:
        pass  # not linked yet
    try:
        fleet_proc.herdr("plugin", "link", ROOT)
        fleet_proc.herdr("server", "reload-config")  # keybindings name plugin actions: the server must see them
    except fleet_proc.HerdrDown as e:
        return f"herdr plugin not linked: {e}"
    m = fleet_manifest.read()
    m["plugin"] = True
    fleet_manifest.write(m)
    return None


def install(argv):
    # --from-herdr: `herdr plugin install` runs this as its build step and owns the plugin itself
    # (linking it again would fight herdr over its location); updates then go through herdr too
    by_herdr = "--from-herdr" in argv
    plan = plan_links()
    print(f"Installing {fleet_version.line()} from {ROOT}\n")
    for path, target, action in plan:
        print(f"  {action:8} {path}" + (f" -> {target}" if action != "conflict" else "  (a file that isn't ours: left alone)"))
    if not by_herdr:
        print(f"  plugin   herdr plugin link {ROOT}")
    print(f"  record   {fleet_manifest.PATH}\n")
    if not fleet_term.confirm("install.sh", "go ahead?", "--yes" in argv):
        print("nothing changed")
        return 3
    conflicts = link()
    fleet_manifest.update(lambda m: m.update(managed_by="herdr" if by_herdr else "install.sh"))
    if by_herdr:
        print("\ndone. herdr manages the plugin and its updates.")
    elif err := link_plugin():
        print(f"\ndone. {err}")
    else:
        print("\ndone. The herdr plugin is linked.")
    if conflicts:
        print(f"left alone (not ours): {', '.join(conflicts)}: remove them and re-run to link fleet's")
    if BIN_DIR not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"\n{BIN_DIR} isn't on PATH: add it in your shell's profile (herdr's popups use a login shell)")
    print("""
Next (each one tells you what it does before doing it):
  fleet setup             propose a config from this machine, then write it
  fleet keys set TYPESAFE_API_KEY   optional: smarter `fleet new` and decision points
  fleet server install    keep the watcher running across reboots
  fleet skills install    teach your agents (Claude Code, Codex, opencode) the fleet
  fleet doctor            check everything; each problem comes with its fix
Keys in herdr: `fleet doctor --only herdr.keys` prints the snippet for ctrl+w f / ctrl+w t.""")
    return 0


def version_key(tag):
    m = TAG.match(tag)
    return tuple(int(x) for x in m.groups()) if m else None


def tags():
    """Release tags, oldest first."""
    return sorted((t for t in git("tag", "--list", "v*").split() if TAG.match(t)), key=version_key)


def changelog(tag, since):
    """The release's CHANGELOG.md sections newer than `since` (a tag, or None for all)."""
    text = git("show", f"{tag}:CHANGELOG.md", check=False)
    if not text:
        return "(no CHANGELOG.md in this release)"
    keep = []
    for section in re.split(r"(?m)^(?=## )", text):
        m = re.match(r"## (v\d+\.\d+\.\d+)", section)
        if not m:
            continue
        if since and version_key(m.group(1)) <= version_key(since):
            break
        keep.append(section.rstrip())
    return "\n\n".join(keep) or "(no new entries)"


def restart_services():
    """Restart every fleet service that's running (systemd or launchd): old code reports stale state."""
    import fleet_server
    notes = []
    for name in fleet_server.SERVICES:
        svc = fleet_server.Service(name)
        try:
            if svc.status()["active"]:
                svc.control("restart")
                notes.append(f"restarted {svc.label}")
        except RuntimeError as e:
            notes.append(f"couldn't restart {svc.label} ({e}): `{svc.cli} restart`")
    return notes


def update_status():
    git("fetch", "-q", "--tags", "origin")
    rel = tags()
    cur = git("describe", "--tags", "--exact-match", "HEAD", check=False) or None
    latest = rel[-1] if rel else None
    ahead = bool(latest) and fleet_proc.run(["git", "-C", ROOT, "merge-base", "--is-ancestor", "HEAD", latest]).returncode != 0
    return {"current": cur or git("rev-parse", "--short", "HEAD"), "on_tag": bool(cur), "latest": latest,
            "up_to_date": (cur == latest) if latest else None, "branch_ahead": ahead}  # None: no releases yet


def preview(st):
    print(f"Update {st['current']} -> {st['latest']}\n\n"
          f"{changelog(st['latest'], st['current'] if st['on_tag'] else None)}\n")
    print("Files that change:\n" + git("diff", "--stat", st["current"], st["latest"]))
    watched = git("diff", st["current"], st["latest"], "--", *WATCHED)
    if watched:
        print("\nSkills and installer changes (what your agents will follow, and what runs on install):\n")
        print(watched)


def apply_update(latest):
    git("checkout", "-q", "--detach", latest)
    link(quiet=True)
    link_plugin()
    import fleet_skills
    fleet_skills.refresh()  # agents follow the release they just saw, not the old copy
    for note in restart_services():
        print(note)
    print(f"herdr-fleet is now {latest}. Run `fleet doctor`.")


def update(argv):
    by_herdr = fleet_manifest.read()["managed_by"] == "herdr"
    if by_herdr and ("--json" in argv or "--check" in argv):
        st = {"managed_by": "herdr", "current": fleet_version.info()["version"], "latest": None, "up_to_date": None}
        print(json.dumps(st) if "--json" in argv else
              f"herdr-fleet {st['current']}, installed by herdr: `herdr plugin update herdr-fleet` checks for updates")
        return 0
    if by_herdr:
        print("herdr installed this copy: update it with `herdr plugin update herdr-fleet` (it shows the "
              "changes first, and re-runs this install)")
        return 0
    if not os.path.exists(os.path.join(ROOT, ".git")):
        print(f"fleet update: {ROOT} isn't a git checkout; reinstall from a clone", file=sys.stderr)
        return 1
    st = update_status()
    if "--json" in argv:
        print(json.dumps(st))
        return 0
    if not st["latest"]:
        print("no releases published yet")
        return 0
    if st["up_to_date"] or "--check" in argv:
        print(f"herdr-fleet {st['current']}; latest release {st['latest']}" +
              (": up to date" if st["up_to_date"] else ": `fleet update` shows what changes"))
        return 0
    if st["branch_ahead"]:
        print(f"fleet update: this install ({st['current']}) has commits the latest release ({st['latest']}) "
              "doesn't; it tracks a branch, not releases. Leaving it alone.", file=sys.stderr)
        return 1
    if git("status", "--porcelain"):  # before the preview: no point reading it if we can't apply it
        print(f"fleet update: {ROOT} has local changes; commit or stash them first", file=sys.stderr)
        return 1
    preview(st)
    if not fleet_term.confirm("fleet update", f"\nmove {ROOT} to {st['latest']}?", "--yes" in argv):
        print("nothing changed")
        return 3
    apply_update(st["latest"])
    return 0


def uninstall(argv):
    import fleet_skills
    m = fleet_manifest.read()
    mine = [p for p in m["links"] if os.path.islink(p) and os.path.realpath(p).startswith(os.path.realpath(ROOT) + os.sep)]
    skills = [d for d in m["skills"] if fleet_skills.installed_version(d) is not None]
    print("Removes:")
    for p in mine:
        print(f"  {p}")
    for d in skills:
        print(f"  {d} (the fleet skill copy)")
    if m.get("plugin"):
        print("  the herdr plugin link (herdr plugin unlink herdr-fleet)")
    print(f"\nKeeps: {fleet_config.DIR} (config, keys), {fleet_config.STATE_DIR} (ledger, reviews), {ROOT} "
          "(this checkout).\nService units are `fleet server uninstall`'s and `fleet remote uninstall`'s to remove.")
    if not fleet_term.confirm("fleet uninstall", "go ahead?", "--yes" in argv):
        print("nothing changed")
        return 3
    for p in mine:
        os.remove(p)
    for d in skills:
        shutil.rmtree(d)
    if m.get("plugin"):
        try:
            fleet_proc.herdr("plugin", "unlink", "herdr-fleet")
        except fleet_proc.HerdrDown as e:
            print(f"herdr plugin not unlinked: {e}")
    m["links"] = [p for p in m["links"] if p not in mine]
    m["skills"] = [d for d in m["skills"] if d not in skills]
    m["plugin"] = False
    fleet_manifest.write(m)
    if m["managed_by"] == "herdr":
        print("done. herdr still has the plugin: `herdr plugin uninstall herdr-fleet` removes it and this copy.")
        print(f"Your config, keys and state stay in {fleet_config.DIR} and {fleet_config.STATE_DIR}.")
        return 0
    print(f"done. To remove everything: rm -rf {ROOT} {fleet_config.DIR} {fleet_config.STATE_DIR}")
    return 0


def main(argv):
    if any(a in ("-h", "--help", "help") for a in argv):
        print(__doc__.strip())
        return 0
    if sys.version_info < (3, 11):
        print("herdr-fleet needs python 3.11+ (tomllib)", file=sys.stderr)
        return 1
    commands = {"install": install, "update": update, "uninstall": uninstall}
    if not argv or argv[0] not in commands:
        print("usage: fleet_install.py install|update|uninstall", file=sys.stderr)
        return 2
    return commands[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
