"""fleet_doctor — is this install able to run a fleet, and if not, what exactly to do.

Read-only: never starts, installs or writes anything. Each check returns results
{"id", "status": ok|warn|fail|skip, "summary", "fix"?, "who"?: agent|human, "detail"?}; `who`
says whether an agent may run the fix (after asking) or only the human can. Checks run in
parallel with a timeout each; one that hangs reports itself, it doesn't hang doctor.
"""
import json, os, re, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

import fleet_config
import fleet_version

TIMEOUT_S = 10
BINS = ("fleet", "ship", "dev", "orch", "fleet-watch", "fleet-cockpit", "fleet-new")


def r(id, status, summary, fix=None, who=None, **detail):
    out = {"id": id, "status": status, "summary": summary}
    if fix:
        out.update(fix=fix, who=who or "human")
    if detail:
        out["detail"] = detail
    return out


def sh(*args, timeout=TIMEOUT_S):
    """(returncode, stdout+stderr) via fleet_proc.run (124 on timeout); 127 when it isn't installed."""
    import fleet_proc
    try:
        p = fleet_proc.run(list(args), timeout=timeout)
    except FileNotFoundError:
        return 127, f"{args[0]}: not found"
    return p.returncode, (p.stdout + p.stderr).strip()


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


# ── checks ───────────────────────────────────────────────────────────────────

def check_python():
    v = sys.version_info
    if v < (3, 11):
        return r("python", "fail", f"python {v.major}.{v.minor}; needs 3.11+ (tomllib)", "install python 3.11 or newer")
    return r("python", "ok", f"python {v.major}.{v.minor}")


def check_bash():
    """orch is bash 4 (associative arrays); macOS's /bin/bash is 3.2."""
    bash = shutil.which("bash")
    code, out = sh(bash, "-c", "echo ${BASH_VERSINFO[0]}") if bash else (127, "")
    major = out if code == 0 else ""
    if not major.isdigit() or int(major) < 4:
        return r("bash", "warn", f"bash {major or '?'} at {bash}: orch needs 4+",
                 "macOS: `brew install bash` and put Homebrew's bin first on PATH")
    return r("bash", "ok", f"bash {major}")


def check_bins():
    here = os.path.join(fleet_version.ROOT, "bin")
    missing = [b for b in BINS if not shutil.which(b)]
    elsewhere = {b: shutil.which(b) for b in BINS if shutil.which(b)
                 and os.path.dirname(os.path.realpath(shutil.which(b))) != os.path.realpath(here)}
    if missing:
        return r("bins", "fail", f"not on PATH: {', '.join(missing)}",
                 f"link {here}/* into a directory on PATH (e.g. ~/.local/bin)", path=os.environ.get("PATH", ""))
    if elsewhere:
        return r("bins", "fail", f"{', '.join(elsewhere)} run from another install than {fleet_version.ROOT}",
                 "remove the other copy or relink: two installs disagree about state and versions", found=elsewhere)
    return r("bins", "ok", f"all {len(BINS)} commands on PATH from {fleet_version.ROOT}")


def check_login_shell():
    # herdr's popups run `bash -lc "exec fleet-cockpit"`: the login profile must find it too
    rc, out = sh("bash", "-lc", "command -v fleet-cockpit")
    if rc:
        return r("bins.login_shell", "warn", "a login shell can't find fleet-cockpit (the herdr popups use one)",
                 "add the directory with fleet's commands to PATH in ~/.profile (or your login shell's profile)")
    return r("bins.login_shell", "ok", "login shell finds the commands")


def judge_herdr(have, need, tested, agents_or_error):
    """have/need/tested: versions; agents_or_error: pane count, or why herdr didn't answer."""
    if vtuple(have) < vtuple(need):
        return r("herdr", "fail", f"herdr {have}; this release needs {need}+", "upgrade herdr")
    if isinstance(agents_or_error, str):
        return r("herdr", "fail", f"herdr {have} isn't answering: {agents_or_error}", "start herdr (or its server)")
    if vtuple(have) > vtuple(tested):
        return r("herdr", "warn", f"herdr {have} is newer than the newest tested ({tested})",
                 "if something breaks, report it with `fleet doctor --json`")
    return r("herdr", "ok", f"herdr {have}, {agents_or_error} agent pane(s)")


def check_herdr():
    rc, out = sh("herdr", "--version")
    if rc == 127:
        return r("herdr", "fail", "herdr isn't installed", "install herdr: https://herdr.dev")
    import fleet_proc
    try:
        panes = len(fleet_proc.herdr_agents())
    except fleet_proc.HerdrDown as e:
        panes = str(e)
    return judge_herdr(out.split()[-1] if out else "0", fleet_version.manifest().get("min_herdr_version", "0"),
                       fleet_version.TESTED_HERDR_MAX, panes)


def plugin_fix():
    import fleet_manifest
    if fleet_manifest.read()["managed_by"] == "herdr":
        return "herdr plugin update herdr-fleet (or reinstall: herdr plugin install <owner>/herdr-fleet)", "human"
    return f"herdr plugin link {fleet_version.ROOT}", "agent"


def check_plugin():
    rc, out = sh("herdr", "plugin", "list")
    if rc == 127:
        return r("herdr.plugin", "skip", "herdr isn't installed")
    m = re.search(r"herdr-fleet .*?(enabled|disabled)\s*\[(?:local:)?([^\]]+)\]", out)
    if not m:
        return r("herdr.plugin", "fail", "the herdr-fleet plugin isn't installed", *plugin_fix())
    if m.group(1) != "enabled":
        return r("herdr.plugin", "fail", "the herdr-fleet plugin is disabled", "herdr plugin enable herdr-fleet", "agent")
    if os.path.realpath(m.group(2)) != os.path.realpath(fleet_version.ROOT):
        return r("herdr.plugin", "fail", f"the plugin points at {m.group(2)}, not this install", *plugin_fix())
    return r("herdr.plugin", "ok", "plugin linked to this install")


def check_herdr_keys():
    if not shutil.which("herdr"):
        return r("herdr.keys", "skip", "herdr isn't installed")
    path = os.path.expanduser("~/.config/herdr/config.toml")
    try:
        import tomllib
        with open(path, "rb") as f:
            cmds = {k.get("command") for k in tomllib.load(f).get("keys", {}).get("command", [])}
    except (OSError, ValueError):
        cmds = set()
    want = [a for a in ("herdr-fleet.open", "herdr-fleet.new") if a not in cmds]
    if want:
        snippet = "".join(f'[[keys.command]]\nkey = "prefix+{k}"\ntype = "plugin_action"\ncommand = "{a}"\n\n'
                          for a, k in (("herdr-fleet.open", "f"), ("herdr-fleet.new", "t")) if a in want)
        return r("herdr.keys", "warn", f"no herdr key for {', '.join(want)}",
                 f"add to {path}, then `herdr server reload-config`:\n{snippet.strip()}")
    return r("herdr.keys", "ok", "cockpit and new-session keys bound")


def used_kinds():
    roles = fleet_config.load().get("roles", {})
    kinds = {x.get("kind") for x in roles.values() if isinstance(x, dict)}
    if "opposite" in kinds:
        kinds |= set(fleet_config.get("agents", "opposite", []))
    return sorted(k for k in kinds if k in fleet_config.agent_kinds())


def check_integrations():
    rc, out = sh("herdr", "integration", "status")
    if rc == 127:
        return [r("herdr.integrations", "skip", "herdr isn't installed")]
    res = []
    for kind in used_kinds():
        m = re.search(rf"^{re.escape(kind)}: (.+?)(?: \(|$)", out, re.M)
        state = m.group(1) if m else "unknown"
        if state.startswith("installed") or state == "unknown":
            res.append(r(f"herdr.integrations.{kind}", "ok", f"{kind}: {state}"))
        else:
            # without it herdr can't tell when the agent is working, idle or blocked
            res.append(r(f"herdr.integrations.{kind}", "warn", f"{kind}: herdr integration {state}",
                         f"herdr integration install {kind}"))
    return res or [r("herdr.integrations", "skip", "no agent roles configured")]


def check_gh():
    rc, out = sh("gh", "auth", "status")
    if rc == 127:
        return r("gh", "fail", "gh (GitHub CLI) isn't installed", "install gh: https://cli.github.com")
    if rc:
        return r("gh", "fail", "gh isn't logged in", "gh auth login")
    scopes = re.search(r"Token scopes: (.+)", out)
    missing = [s for s in ("repo", "read:org") if scopes and f"'{s}'" not in scopes.group(1)]
    if missing:
        return r("gh", "warn", f"gh token lacks {', '.join(missing)}", f"gh auth refresh -s {','.join(missing)}")
    return r("gh", "ok", "gh logged in")


LOGIN = {"claude": ("claude", "auth", "status"), "codex": ("codex", "login", "status"),
         "opencode": ("opencode", "auth", "list")}


def check_agents():
    driver = fleet_config.load().get("roles", {}).get("driver", {}).get("kind")
    res = []
    for kind in used_kinds():
        cmd = fleet_config.agent_kinds()[kind]["cmd"][0]
        level = "fail" if kind == driver else "warn"
        if not shutil.which(cmd):
            res.append(r(f"agents.{kind}", level, f"{cmd} isn't on PATH ({kind} is a configured role)",
                         f"install {kind}, or change [roles] in the fleet config"))
            continue
        rc, _ = sh(*LOGIN[kind]) if kind in LOGIN else (0, "")
        if rc and rc != 127:
            res.append(r(f"agents.{kind}", "warn", f"{kind} doesn't look logged in", f"log in: {' '.join(LOGIN[kind][:1])}"))
        else:
            res.append(r(f"agents.{kind}", "ok", f"{kind} ready" + (" (driver)" if kind == driver else "")))
    return res


def check_codex_sandbox():
    if "codex" not in used_kinds():
        return r("codex.sandbox", "skip", "codex isn't a configured role")
    if not shutil.which("codex"):
        return r("codex.sandbox", "skip", "codex isn't installed (see the agents check)")
    rc, out = sh("codex", "sandbox", "--", "true", timeout=60)
    if rc or "bwrap:" in out:
        return r("codex.sandbox", "warn", "codex can't sandbox here, so it can't be a reviewer (craft reviews use claude)",
                 "Ubuntu 24.04: allow bubblewrap user namespaces (an AppArmor profile for bwrap, or "
                 "sysctl kernel.apparmor_restrict_unprivileged_userns=0)", first_line=out.splitlines()[0] if out else "")
    return r("codex.sandbox", "ok", "codex sandbox works")


def check_repos():
    rc, out = sh("dev", "repos", "--json")
    if rc:
        return r("repos", "fail", out.splitlines()[-1][:200] if out else "dev repos failed",
                 'set [dev] repos_dir = "<folder of your git checkouts>" in ~/.config/fleet/config.toml')
    repos = json.loads(out)["repos"]
    return r("repos", "ok", f"{len(repos)} repos", count=len(repos), dir=fleet_config.get("dev", "repos_dir", ""))


def check_config():
    if not os.path.exists(fleet_config.PATH):
        return [r("config", "warn", "no config file: built-in defaults in effect",
                  "run `fleet setup` (or write ~/.config/fleet/config.toml)")]
    try:
        problems = fleet_config.check_files()
        ignored = fleet_config.resolve().ignored
    except fleet_config.ConfigError as e:
        return [r("config", "fail", str(e), "fix the TOML at the position shown")]
    res = [r("config", "ok", f"{fleet_config.PATH} parses")]
    for p in problems:
        res.append(r(f"config.schema.{p['key']}", p["level"], f"{p['key']}: {p['msg']}", f"edit {p['file']}"))
    for key, layer, why in ignored:
        res.append(r(f"config.ignored.{key}", "warn", f"{key} from {layer} ignored: {why}", f"edit the {layer} config"))
    return res


def check_keys():
    import fleet_keys
    st = fleet_keys.status()  # names and presence only; values never leave fleet_keys
    res = []
    if st["exists"]:
        res.append(r("keys.mode", "ok", f"keys file is {st['mode']}") if st["ok_mode"] else
                   r("keys.mode", "fail", f"{st['file']} is {st['mode']}: readable by others",
                     f"chmod 600 {st['file']}", "agent"))
    have = st["keys"]
    if not any(have.values()):
        res.append(r("keys.present", "warn", "no model keys: fleet new routes with rules, decision points stay off",
                     "set one: `fleet keys set TYPESAFE_API_KEY` (or OPENAI_API_KEY)", keys=have))
    else:
        res.append(r("keys.present", "ok", "keys: " + ", ".join(k for k, v in have.items() if v), keys=have))
    dec = fleet_config.load().get("decisions", {})
    on = [p for p, v in dec.items() if v in ("shadow", "act") and p != "model"]
    if on and not have.get("TYPESAFE_API_KEY"):
        res.append(r("keys.decisions", "warn", f"decision points {', '.join(on)} are on but TYPESAFE_API_KEY isn't set",
                     "fleet keys set TYPESAFE_API_KEY"))
    return res


def check_watch():
    res = []
    if sys.platform == "darwin":
        plist = os.path.expanduser("~/Library/LaunchAgents/dev.herdr-fleet.watch.plist")
        installed = os.path.exists(plist)
        res.append(r("watch.unit", "ok" if installed else "warn",
                     "launchd agent installed" if installed else "no launchd agent: the watcher stops when you log out",
                     None if installed else "fleet server install"))
    else:
        rc, out = sh("systemctl", "--user", "is-enabled", "fleet-watch")
        if rc == 127:
            res.append(r("watch.unit", "skip", "no systemd"))
        elif out.strip() not in ("enabled", "static"):
            res.append(r("watch.unit", "warn", "no fleet-watch user unit: the watcher won't survive a reboot",
                         "fleet server install"))
        else:
            rc, out = sh("systemctl", "--user", "is-active", "fleet-watch")
            res.append(r("watch.unit", "ok" if out.strip() == "active" else "fail",
                         f"fleet-watch unit {out.strip()}", None if out.strip() == "active" else "systemctl --user start fleet-watch"))
    import fleet_serve
    try:
        ping = fleet_serve.call("ping", timeout=3)
    except Exception as e:
        ping = e
    return res + [judge_watcher(ping, fleet_version.info(), fleet_config.get("watch", "interval", 120), time.time())]


def judge_watcher(ping, mine, interval, now):
    """ping: the server's answer, None (not running) or the error it gave."""
    if isinstance(ping, Exception):
        return r("watch.alive", "fail", f"the fleet server answered with an error: {ping}", "fleet server restart")
    if ping is None:
        return r("watch.alive", "fail", "the fleet server isn't running",
                 "fleet server start if the watch.unit check is ok; otherwise fleet server install "
                 "(or `fleet watch` in a pane)")
    last = ping.get("last_pass")
    if last and now - _ts(last) > 3 * interval:
        return r("watch.alive", "fail", f"the watcher's last pass was {int((now - _ts(last)) // 60)} min ago",
                 "fleet server restart")
    if ping.get("version") and (ping["version"], ping.get("commit")) != (mine["version"], mine["commit"]):
        return r("watch.alive", "warn", f"the running watcher is {ping['version']} ({ping.get('commit')}); "
                 f"installed is {mine['version']} ({mine['commit']})", "fleet server restart", "agent")
    return r("watch.alive", "ok", f"watcher up since {ping.get('started_at')}, events "
             f"{'connected' if ping.get('events') else 'not connected'}")


def _ts(iso):
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def check_skill():
    import fleet_skills
    st = fleet_skills.status()
    if not st["harnesses"]:
        homes = ", ".join(home for home, _ in fleet_skills.HARNESSES.values())
        return r("skill", "skip", f"no agent harness folders ({homes})")
    res = []
    for h, x in st["harnesses"].items():
        fix = f"fleet skills install --to {h}"
        missing = [n for n, s in x["skills"].items() if not s["installed"] and not s["symlink"]]
        if x["symlink"]:
            linked = ", ".join(n for n, s in x["skills"].items() if s["symlink"])
            res.append(r(f"skill.{h}", "warn", f"{h}: the {linked} skill is a symlink, so it changes whenever that "
                         "checkout does (or points at nothing)", fix))
        elif x["current"]:
            res.append(r(f"skill.{h}", "ok", f"{h}: fleet skills {x['installed']}"))
        elif not x["installed"]:
            res.append(r(f"skill.{h}", "warn", f"{h}: no fleet skill, so its agents don't know the fleet's rules", fix))
        elif missing:
            res.append(r(f"skill.{h}", "warn", f"{h}: no {', '.join(missing)} skill", fix))
        else:
            res.append(r(f"skill.{h}", "warn", f"{h}: fleet skills are {x['installed']}, installed tools are {st['version']}",
                         fix))
    return res


CHECKS = [("python", check_python), ("bash", check_bash), ("bins", check_bins), ("bins.login_shell", check_login_shell),
          ("herdr", check_herdr), ("herdr.plugin", check_plugin), ("herdr.keys", check_herdr_keys),
          ("herdr.integrations", check_integrations), ("gh", check_gh), ("agents", check_agents),
          ("codex.sandbox", check_codex_sandbox), ("repos", check_repos), ("config", check_config),
          ("keys", check_keys), ("watch", check_watch), ("skill", check_skill)]


def selects(id, wanted):
    """`herdr` selects herdr and herdr.*; `herdr.plugin` selects only that one."""
    return any(id == w or id.startswith(w + ".") for w in wanted)


def may_produce(check_id, wanted):
    """A check can report ids below its own: `keys` reports keys.mode, so `--only keys.mode` runs it."""
    return selects(check_id, wanted) or any(w.startswith(check_id + ".") for w in wanted)


def run(only=None):
    def one(check):
        id, fn = check
        try:
            got = fn()
        except Exception as e:  # a broken check is a finding, not a crash
            got = r(id, "fail", f"check crashed: {type(e).__name__}: {e}"[:200])
        return got if isinstance(got, list) else [got]
    picked = [c for c in CHECKS if not only or may_produce(c[0], only)]
    with ThreadPoolExecutor(8) as ex:
        results = [x for got in ex.map(one, picked) for x in got]
    return [x for x in results if not only or selects(x["id"], only)]


def main(argv):
    only, rest = None, list(argv)
    while rest:
        a = rest.pop(0)
        if a == "--only" and rest and not rest[0].startswith("-"):
            only = rest.pop(0).split(",")
        elif a != "--json":  # a typo'd --json must not quietly produce text
            print(f"fleet doctor: bad argument {a!r}; usage: fleet doctor [--json] [--only id,...]", file=sys.stderr)
            return 2
    if only:
        unknown = [o for o in only if not any(may_produce(i, [o]) for i, _ in CHECKS)]
        if unknown:
            print(f"fleet doctor: unknown check {', '.join(unknown)} (have: {', '.join(i for i, _ in CHECKS)})",
                  file=sys.stderr)
            return 2
    results = run(only)
    failed = any(x["status"] == "fail" for x in results)
    if "--json" in argv:
        print(json.dumps({"version": fleet_version.info()["version"], "ok": not failed, "checks": results}, indent=1))
    else:
        for x in results:
            print(f"{x['status']:4}  {x['id']:28} {x['summary']}")
        fixes = [x for x in results if x.get("fix") and x["status"] in ("warn", "fail")]
        if fixes:
            print()
            for x in fixes:
                fix = x["fix"].replace("\n", "\n      ")
                print(f"{x['id']} — fix ({x['who']}): {fix}")
        counts = {s: sum(1 for x in results if x["status"] == s) for s in ("ok", "warn", "fail", "skip")}
        print(f"\n{counts['ok']} ok, {counts['warn']} warn, {counts['fail']} fail" +
              (f", {counts['skip']} skipped" if counts["skip"] else ""))
    return 1 if failed else 0
