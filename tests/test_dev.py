"""dev: registry discovery, context create/reopen, cross, done, prune safety, pr-checkout.
Run: python3 -m unittest discover -s tests   (no network: herdr, gh and npm are fake
executables on PATH; repos are temp git repos cloned from local bare "origins")."""
import importlib.machinery, importlib.util, json, os, shutil, subprocess, sys, tempfile, textwrap, time, unittest
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEV = os.path.join(ROOT, "bin", "dev")
GIT_DIR = os.path.dirname(shutil.which("git") or "/usr/bin/git")

# Fake herdr: records argv to $FAKE_HERDR_LOG, keeps workspaces/tabs in $FAKE_HERDR_STATE and
# creates/removes real git worktrees the way `herdr worktree create/remove` does.
FAKE_HERDR = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
state_path, args = os.environ["FAKE_HERDR_STATE"], sys.argv[1:]
st = json.loads(Path(state_path).read_text()) if os.path.exists(state_path) else {}
st.setdefault("workspaces", []); st.setdefault("tabs", {}); st.setdefault("agents", []); st.setdefault("next", 1)
with open(os.environ["FAKE_HERDR_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
def opt(name):
    return args[args.index(name) + 1] if name in args else None
def out(obj):
    print(json.dumps(obj))
def save():
    json.dump(st, open(state_path, "w"))
def new_ws(label, repo_root, path):
    wid = f"w{st['next']}"; st["next"] += 1
    st["workspaces"].append({"workspace_id": wid, "label": label, "agent_status": "idle",
                             "worktree": {"repo_root": repo_root, "checkout_path": path, "is_linked_worktree": True}})
    st["tabs"][wid] = [{"tab_id": f"{wid}:t1", "label": "1"}]
    save()
    out({"result": {"workspace": {"workspace_id": wid}}})
def git(*a):
    p = subprocess.run(["git", *a], capture_output=True, text=True)
    if p.returncode:
        sys.stderr.write(p.stderr); sys.exit(1)
cmd = tuple(args[:2])
if cmd == ("workspace", "list"):
    out({"result": {"workspaces": st["workspaces"]}})
elif cmd == ("worktree", "create"):
    git("-C", opt("--cwd"), "worktree", "add", "-b", opt("--branch"), opt("--path"), opt("--base"))
    new_ws(opt("--label"), opt("--cwd"), opt("--path"))
elif cmd == ("worktree", "open"):
    new_ws(opt("--label"), opt("--cwd"), opt("--path"))
elif cmd == ("worktree", "remove"):
    ws = next(w for w in st["workspaces"] if w["workspace_id"] == opt("--workspace"))
    git("-C", ws["worktree"]["repo_root"], "worktree", "remove", *(["--force"] if "--force" in args else []),
        ws["worktree"]["checkout_path"])
    st["workspaces"].remove(ws); save()
elif cmd == ("worktree", "list"):
    out({"result": {"source": {"source_workspace_id": st.get("parent")}}})
elif cmd == ("workspace", "close"):
    st["workspaces"] = [w for w in st["workspaces"] if w["workspace_id"] != args[2]]; save()
elif cmd == ("tab", "list"):
    out({"result": {"tabs": st["tabs"].get(opt("--workspace"), [])}})
elif cmd == ("tab", "rename"):
    for tabs in st["tabs"].values():
        for t in tabs:
            if t["tab_id"] == args[2]:
                t["label"] = args[3]
    save()
elif cmd == ("tab", "create"):
    wid = opt("--workspace"); tabs = st["tabs"].setdefault(wid, [])
    tab = {"tab_id": f"{wid}:t{len(tabs) + 1}", "label": opt("--label")}
    tabs.append(tab); save()
    out({"result": {"tab": tab, "root_pane": {"pane_id": tab["tab_id"] + ":p1"}}})
elif cmd == ("agent", "list"):
    out({"result": {"agents": st["agents"]}})
elif cmd == ("pane", "list"):
    out({"result": {"panes": []}})
elif cmd in (("workspace", "focus"), ("pane", "rename")):
    pass
else:
    sys.exit(2)
'''

# Fake gh: `pr list` and `pr view` answer from $FAKE_GH_STATE; anything else fails.
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
st = json.loads(Path(os.environ["FAKE_GH_STATE"]).read_text()) if os.path.exists(os.environ["FAKE_GH_STATE"]) else {}
args = sys.argv[1:]
if args[:2] == ["pr", "list"]:
    print(json.dumps(st.get("prs", [])))
elif args[:2] == ["pr", "view"] and "view" in st:
    print(json.dumps(st["view"]))
else:
    sys.exit(1)
'''

FAKE_NPM = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_NPM_LOG"], "a") as f:
    f.write(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd()}) + "\n")
'''


def load_dev_module():
    loader = importlib.machinery.SourceFileLoader("dev_cli", DEV)
    spec = importlib.util.spec_from_loader("dev_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dev_cli"] = mod  # dataclasses look their module up here
    loader.exec_module(mod)
    return mod


class DevTestCase(unittest.TestCase):
    """Temp world: $HOME, a repos dir with `platform` (bare-style canonical, base main) and
    `web` (plain clone, base develop), both cloned from local bare origins."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="dev-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.p("home")
        self.repos_dir = self.p("repos")
        self.fakebin = self.p("fakebin")
        for d in (self.home, self.repos_dir, self.fakebin):
            os.makedirs(d)
        for name, body in (("herdr", FAKE_HERDR), ("gh", FAKE_GH), ("npm", FAKE_NPM)):
            with open(os.path.join(self.fakebin, name), "w") as f:
                f.write(body)
            os.chmod(os.path.join(self.fakebin, name), 0o755)
        self.env = {
            "PATH": os.pathsep.join([self.fakebin, GIT_DIR, os.path.dirname(sys.executable), "/usr/bin", "/bin"]),
            "HOME": self.home, "LANG": "C.UTF-8",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_NOSYSTEM": "1",
            "DEV_SKIP_INSTALL": "1", "DEV_NO_FOCUS": "",
            "FAKE_HERDR_STATE": self.p("herdr.json"), "FAKE_HERDR_LOG": self.p("herdr.log"),
            "FAKE_GH_STATE": self.p("gh.json"), "FAKE_NPM_LOG": self.p("npm.log"),
        }
        self.platform_origin = self.make_origin("platform", "main", {"README.md": "platform\n",
                                                                     "package-lock.json": "{}\n"})
        self.make_origin("web", "develop", {"README.md": "web\n"})
        self.platform = self.p("repos", "platform")
        self.web = self.p("repos", "web")
        # bare-style canonical: platform/.git is a bare clone, no working tree
        self.git("clone", "-q", "--bare", self.platform_origin, os.path.join(self.platform, ".git"))
        self.git("-C", self.platform, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        self.git("-C", self.platform, "fetch", "-q", "origin")
        self.git("-C", self.platform, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.git("clone", "-q", self.p("origin", "web.git"), self.web)
        os.makedirs(self.p("repos", "notes"))  # not a git checkout: ignored
        self.config(f'[dev]\nrepos_dir = "{self.repos_dir}"\n')
        # every repo's post-checkout hook fails, as husky's `yarn yarnhook` does in a worktree with no
        # node_modules yet: every test that makes a worktree checks that dev doesn't run it
        for git_dir in (os.path.join(self.platform, ".git"), os.path.join(self.web, ".git")):
            self.write(os.path.join(git_dir, "hooks", "post-checkout"), "#!/bin/sh\nexit 1\n")
            os.chmod(os.path.join(git_dir, "hooks", "post-checkout"), 0o755)

    # ------------------------------------------------------------ helpers

    def p(self, *parts):
        return os.path.join(self.tmp, *parts)

    def git(self, *args, cwd=None):
        """git for arranging and inspecting; hooks off like dev's own (the fixture's hook fails)."""
        r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd or self.tmp, env=self.env, capture_output=True, text=True)
        if r.returncode:
            raise AssertionError(f"git {' '.join(args)}: {r.stderr}")
        return r.stdout.strip()

    def make_origin(self, name, branch, files):
        work = self.p("seedwork", name)
        os.makedirs(work)
        self.git("init", "-q", "-b", branch, work)
        for rel, body in files.items():
            with open(os.path.join(work, rel), "w") as f:
                f.write(body)
        self.git("-C", work, "add", "-A")
        self.git("-C", work, "commit", "-qm", "init")
        if name == "platform":
            self.git("-C", work, "checkout", "-qb", "feature-x")
            self.git("-C", work, "commit", "-q", "--allow-empty", "-m", "feature")
            self.git("-C", work, "checkout", "-q", branch)
        origin = self.p("origin", f"{name}.git")
        self.git("clone", "-q", "--bare", work, origin)
        return origin

    def dev(self, *args, cwd=None, cmd=None, **env):
        r = subprocess.run([*(cmd or [sys.executable, DEV]), *args], cwd=cwd or self.tmp,
                           env={**self.env, **env}, capture_output=True, text=True, timeout=60)
        return r

    def ok(self, *args, **kw):
        r = self.dev(*args, **kw)
        self.assertEqual(r.returncode, 0, f"dev {args}: {r.stdout}\n{r.stderr}")
        return r

    def herdr_calls(self):
        if not os.path.exists(self.env["FAKE_HERDR_LOG"]):
            return []
        with open(self.env["FAKE_HERDR_LOG"]) as f:
            return [json.loads(line) for line in f]

    def clear_calls(self):
        if os.path.exists(self.env["FAKE_HERDR_LOG"]):
            os.remove(self.env["FAKE_HERDR_LOG"])

    def herdr_state(self):
        with open(self.env["FAKE_HERDR_STATE"]) as f:
            return json.load(f)

    def set_herdr_state(self, **kw):
        st = self.herdr_state() if os.path.exists(self.env["FAKE_HERDR_STATE"]) else {}
        st.update(kw)
        with open(self.env["FAKE_HERDR_STATE"], "w") as f:
            json.dump(st, f)

    def set_gh(self, **kw):
        with open(self.env["FAKE_GH_STATE"], "w") as f:
            json.dump(kw, f)

    def branch_exists(self, repo, branch):
        return subprocess.run(["git", "-C", repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                              env=self.env).returncode == 0

    def config(self, body):
        self.write(os.path.join(self.home, ".config", "fleet", "config.toml"), body)

    def write(self, path, body="x\n"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(body)


class Registry(DevTestCase):
    def test_scan_finds_git_dirs_only(self):
        self.git("-C", self.platform, "worktree", "add", "-q", self.p("repos", "platform-wt-x"), "-b", "platform__x",
                 "origin/main")
        os.makedirs(self.p("repos", ".spares"))
        self.git("-C", self.platform, "worktree", "add", "-q", self.p("repos", ".spares", "platform"), "-b",
                 "spare", "origin/main")
        out = json.loads(self.ok("repos", "--json").stdout)
        self.assertEqual(out, {"repos": [
            {"alias": "platform", "path": self.platform, "defaultBranch": "main"},
            {"alias": "web", "path": self.web, "defaultBranch": "develop"},
        ]})
        lines = self.ok("repos").stdout.splitlines()
        self.assertEqual(lines[1], f"{'web'.ljust(40)} {'develop'.ljust(8)} {self.web}")

    def test_config_repos_add_and_override(self):
        self.config(f'[dev]\nrepos_dir = "{self.repos_dir}"\n'
                    f'[dev.repos.web]\npath = "{self.web}"\nbase = "main"\n'
                    '[dev.repos.other]\npath = "~/custom/other"\nbase = "release"\n'
                    '[dev.repos.bare]\npath = "/x/bare"\n'
                    f'[dev.repos.site]\npath = "{self.web}"\n')
        repos = {r["alias"]: r for r in json.loads(self.ok("repos", "--json").stdout)["repos"]}
        self.assertEqual(sorted(repos), ["bare", "other", "platform", "site", "web"])
        self.assertEqual(repos["site"]["defaultBranch"], "develop")  # no base: read from origin's HEAD
        self.assertEqual(repos["web"]["defaultBranch"], "main")
        self.assertEqual(repos["other"]["path"], os.path.join(self.home, "custom", "other"))
        self.assertEqual(repos["other"]["defaultBranch"], "release")
        self.assertEqual(repos["bare"]["defaultBranch"], "main")

        self.config('[dev.repos.lonely]\nbase = "main"\n')
        r = self.dev("repos")
        self.assertEqual(r.returncode, 1)
        self.assertIn("[dev.repos.lonely] needs path", r.stderr)

    def test_a_repos_dir_that_doesnt_exist_says_so(self):
        self.config('[dev]\nrepos_dir = "~/nowhere"\n')
        r = self.dev("repos")
        self.assertEqual(r.returncode, 1)
        self.assertIn("repos_dir ~/nowhere doesn't exist", r.stderr)

    def test_env_and_tsv_are_not_read(self):
        """Only the config names repos: the watcher and herdr popups don't share a shell's env."""
        self.config(f'[dev]\nrepos_dir = "{self.home}"\n')
        self.write(self.p("repos.tsv"), f"web\t{self.web}\tmain\n")
        r = self.dev("repos", DEV_REPOS_DIR=self.repos_dir, DEV_REPOS_FILE=self.p("repos.tsv"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("No repos found: set [dev] repos_dir", r.stderr)

    def test_knows(self):
        self.assertEqual(self.dev("--knows", "platform").returncode, 0)
        self.assertEqual(self.dev("--knows", "nope").returncode, 1)
        self.assertEqual(self.dev("--knows").returncode, 1)
        self.assertEqual(self.dev("--knows", "constructor").returncode, 1)

    def test_normalize_task(self):
        dev = load_dev_module()
        self.assertEqual(dev.normalize_task("  Fix Auth!! now "), "fix-auth-now")
        self.assertEqual(dev.normalize_task("ACH_debit--50"), "ach_debit-50")
        with self.assertRaises(SystemExit):
            dev.normalize_task(" !!! ")


class SingleRepo(DevTestCase):
    def test_create(self):
        seed = os.path.join(self.home, ".config", "dev", "seed", "platform")
        self.write(os.path.join(seed, ".env"), "SECRET=1\n")
        self.write(os.path.join(seed, "config", "local.json"), "{}\n")
        self.write(os.path.join(seed, "README.md"), "seeded\n")  # tracked file: never overwritten
        r = self.ok("platform", "Fix Auth!")
        wt = self.platform + "-wt-fix-auth"
        self.assertEqual(self.git("-C", wt, "branch", "--show-current"), "platform__fix-auth")
        calls = self.herdr_calls()
        self.assertEqual(self.git("-C", wt, "rev-parse", "HEAD"), self.git("-C", self.platform, "rev-parse", "origin/main"))
        self.assertIn(["worktree", "open", "--cwd", self.platform, "--path", wt, "--label", "fix-auth", "--no-focus",
                       "--json"], calls)
        self.assertIn(["tab", "rename", "w1:t1", "shell"], calls)
        self.assertIn(["tab", "create", "--workspace", "w1", "--label", "agent", "--cwd", wt, "--no-focus"], calls)
        self.assertIn(["pane", "rename", "w1:t2:p1", "platform__fix-auth"], calls)
        self.assertEqual(calls[-1], ["workspace", "focus", "w1"])
        self.assertEqual(Path(wt, ".env").read_text(), "SECRET=1\n")
        self.assertTrue(os.path.exists(os.path.join(wt, "config", "local.json")))
        self.assertEqual(Path(wt, "README.md").read_text(), "platform\n")
        self.assertIn("seed: copied 2 file(s)", r.stdout)
        self.assertIn("DEV_SKIP_INSTALL=1", r.stdout)
        self.assertFalse(os.path.exists(self.env["FAKE_NPM_LOG"]))

    def test_create_copies_warm_caches_from_the_newest_sibling(self):
        self.config(f'[dev]\nrepos_dir = "{self.repos_dir}"\nwarm_caches = [".cache/typecheck"]\n')
        for name, age in (("old", 100), ("new", 10)):
            wt = self.p("repos", f"platform-wt-{name}")
            self.git("-C", self.platform, "worktree", "add", "-q", wt, "-b", f"platform__{name}", "origin/main")
            info = os.path.join(wt, ".cache", "typecheck", "root.tsbuildinfo")
            self.write(info, f"{name}\n")
            os.utime(info, (time.time() - age, time.time() - age))
        r = self.ok("platform", "fresh")
        wt = self.platform + "-wt-fresh"
        self.assertEqual(Path(wt, ".cache", "typecheck", "root.tsbuildinfo").read_text(), "new\n")
        self.assertIn(f"warm: copied .cache/typecheck from {self.p('repos', 'platform-wt-new')}", r.stdout)

    def test_create_runs_lockfile_install(self):
        r = self.ok("platform", "deps", DEV_SKIP_INSTALL="")
        self.assertIn("bootstrap: found package-lock.json, running npm ci", r.stdout)
        with open(self.env["FAKE_NPM_LOG"]) as f:
            self.assertEqual(json.loads(f.readline()), {"args": ["ci"], "cwd": self.platform + "-wt-deps"})

    def test_reopen_live_context_only_focuses(self):
        self.ok("platform", "fix")
        self.clear_calls()
        self.ok("platform", "fix")
        self.assertEqual(self.herdr_calls(), [["workspace", "list"], ["workspace", "focus", "w1"]])
        self.clear_calls()
        self.ok("platform", "fix", DEV_NO_FOCUS="1")
        self.assertEqual(self.herdr_calls(), [["workspace", "list"]])

    def test_reopen_existing_path_opens_worktree(self):
        wt = self.platform + "-wt-old"
        self.git("-C", self.platform, "worktree", "add", "-q", wt, "-b", "platform__old", "origin/main")
        self.ok("platform", "old", DEV_NO_FOCUS="1")
        calls = self.herdr_calls()
        self.assertNotIn("create", [c[1] for c in calls if c[0] == "worktree"])
        self.assertIn(["worktree", "open", "--cwd", self.platform, "--path", wt, "--label", "old", "--no-focus",
                       "--json"], calls)
        self.assertIn(["pane", "rename", "w1:t2:p1", "platform__old"], calls)

    def test_unknown_repo(self):
        r = self.dev("nope", "task")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Unknown repo 'nope'", r.stderr)


class Cross(DevTestCase):
    def test_cross_create(self):
        self.ok("cross", "Big Thing", "platform", "web", "platform", DEV_NO_FOCUS="1")
        pwt, wwt = self.platform + "-wt-big-thing", self.web + "-wt-big-thing"
        calls = self.herdr_calls()
        self.assertEqual(self.git("-C", pwt, "branch", "--show-current"), "big-thing")
        self.assertIn(["worktree", "open", "--cwd", self.platform, "--path", pwt, "--label", "big-thing", "--no-focus",
                       "--json"], calls)
        self.assertEqual(self.git("-C", wwt, "branch", "--show-current"), "big-thing")
        self.assertEqual(self.git("-C", wwt, "rev-parse", "HEAD"), self.git("-C", self.web, "rev-parse", "origin/develop"))
        self.assertIn(["tab", "create", "--workspace", "w1", "--label", "web", "--cwd", wwt, "--no-focus"], calls)
        self.assertIn(["tab", "create", "--workspace", "w1", "--label", "agent", "--cwd", pwt, "--no-focus"], calls)
        self.assertIn(["pane", "rename", "w1:t3:p1", "big-thing"], calls)

        # done removes the sibling too; the shared branch is merged (no commits) and deleted
        r = self.ok("done", "big-thing")
        self.assertIn(f"dev: removing {wwt} (branch big-thing kept)", r.stdout)
        self.assertFalse(os.path.exists(pwt) or os.path.exists(wwt))

    def test_live_cross_sibling_is_not_stale(self):
        self.ok("cross", "both", "platform", "web", DEV_NO_FOCUS="1")
        r = self.ok("prune", "--dry-run")
        self.assertNotIn("web-wt-both", r.stdout)


class Done(DevTestCase):
    def test_clean_merged_removes_and_deletes_branch(self):
        self.ok("platform", "fix", DEV_NO_FOCUS="1")
        wt = self.platform + "-wt-fix"
        r = self.ok("done", "platform__fix")
        self.assertIn(f"dev: removing {wt} (branch platform__fix merged, deleting)", r.stdout)
        self.assertFalse(os.path.exists(wt))
        self.assertFalse(self.branch_exists(self.platform, "platform__fix"))
        self.assertEqual(self.herdr_state()["workspaces"], [])

    def test_dirty_refuses_without_force(self):
        self.ok("platform", "fix", DEV_NO_FOCUS="1")
        wt = self.platform + "-wt-fix"
        self.write(os.path.join(wt, "scratch.txt"))
        r = self.dev("done", "fix")
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"Uncommitted changes in {wt}", r.stderr)
        self.assertTrue(os.path.exists(os.path.join(wt, "scratch.txt")))
        self.ok("done", "fix", "--force")
        self.assertFalse(os.path.exists(wt))

    def test_unmerged_branch_kept_unless_pr_merged(self):
        for task, prs, kept in (("a", [], True), ("b", [{"headRefName": "platform__b", "state": "CLOSED"}], True),
                                ("c", [{"headRefName": "platform__c", "state": "MERGED"},
                                       {"headRefName": "platform__c", "state": "CLOSED"}], False)):
            self.set_gh(prs=prs)
            self.ok("platform", task, DEV_NO_FOCUS="1")
            wt = f"{self.platform}-wt-{task}"
            self.git("-C", wt, "commit", "-q", "--allow-empty", "-m", "work")
            self.ok("done", task)
            self.assertFalse(os.path.exists(wt))
            self.assertEqual(self.branch_exists(self.platform, f"platform__{task}"), kept, task)

    def test_keep_branch(self):
        self.ok("platform", "fix", DEV_NO_FOCUS="1")
        self.ok("done", "fix", "--keep-branch")
        self.assertTrue(self.branch_exists(self.platform, "platform__fix"))

    def test_no_context(self):
        r = self.dev("done", "ghost")
        self.assertEqual(r.returncode, 1)
        self.assertIn("No current context found", r.stderr)


class Prune(DevTestCase):
    def setUp(self):
        super().setUp()
        self.ok("platform", "live", DEV_NO_FOCUS="1")  # tracked by a workspace
        add = lambda path, branch: self.git("-C", self.platform, "worktree", "add", "-q", path, "-b", branch,
                                            "origin/main")
        self.clean, self.dirty = self.platform + "-wt-clean", self.platform + "-wt-dirty"
        self.agent_wt = self.platform + "-wt-agent"
        self.elsewhere = self.p("elsewhere", "platform-review")
        self.spare = self.p("repos", ".spares", "platform")
        add(self.clean, "platform__clean")
        add(self.dirty, "platform__dirty")
        add(self.agent_wt, "platform__agent")
        add(self.elsewhere, "review")
        add(self.spare, "spare")
        self.write(os.path.join(self.dirty, "wip.txt"))
        self.git("-C", self.platform, "branch", "platform__orphan", "origin/main")  # merged, not checked out
        self.git("-C", self.platform, "branch", "random", "origin/main")            # merged, not dev-owned
        self.set_herdr_state(agents=[{"workspace_id": "w1", "cwd": self.platform + "-wt-live"},
                                     {"workspace_id": "w99", "cwd": os.path.join(self.agent_wt, "src")}])

    def test_dry_run_reports_and_touches_nothing(self):
        r = self.ok("prune", "--dry-run")
        self.assertIn(f"prune: removing {self.clean}", r.stdout)
        self.assertIn("prune: deleting merged branch platform__clean", r.stdout)
        self.assertIn(f"prune: skipping {self.dirty} (uncommitted changes)", r.stdout)
        self.assertIn("prune: deleting merged branch platform:platform__orphan", r.stdout)
        for untouched in (self.agent_wt, self.elsewhere, self.spare, "random", "-wt-live"):
            self.assertNotIn(untouched, r.stdout)
        self.assertIn("prune: 1 worktree, 2 branches, 0 workspaces would be removed; 1 dirty skipped", r.stdout)
        for path in (self.clean, self.dirty, self.agent_wt, self.elsewhere, self.spare):
            self.assertTrue(os.path.exists(path), path)
        for branch in ("platform__clean", "platform__orphan", "random"):
            self.assertTrue(self.branch_exists(self.platform, branch), branch)

    def test_real_prune_keeps_dirty(self):
        r = self.ok("prune")
        self.assertIn("prune: 1 worktree, 2 branches, 0 workspaces  removed; 1 dirty skipped", r.stdout)
        self.assertFalse(os.path.exists(self.clean))
        self.assertFalse(self.branch_exists(self.platform, "platform__orphan"))
        for path in (self.dirty, self.agent_wt, self.elsewhere, self.spare, self.platform + "-wt-live"):
            self.assertTrue(os.path.exists(path), path)
        self.assertTrue(os.path.exists(os.path.join(self.dirty, "wip.txt")))
        self.assertTrue(self.branch_exists(self.platform, "random"))

    def test_idle_merged_workspace_closed(self):
        self.set_herdr_state(agents=[])  # nobody in w1 any more; its branch has no unmerged work
        live = self.platform + "-wt-live"
        r = self.ok("prune", "--dry-run")
        self.assertIn(f"prune: closing workspace live and removing {live} (branch platform__live merged)", r.stdout)
        self.assertTrue(os.path.exists(live))
        self.ok("prune")
        self.assertFalse(os.path.exists(live))
        self.assertEqual(self.herdr_state()["workspaces"], [])

    def test_ls_counts_stale(self):
        r = self.ok("ls")
        self.assertIn(f"{'platform__live'.ljust(40)} {'idle'.ljust(8)} {self.platform}-wt-live", r.stdout)
        self.assertIn("dev: 2 stale worktrees on disk", r.stdout)


class CustomTemplates(DevTestCase):
    """~/.config/fleet/config.toml moves worktrees elsewhere and renames branches; dev follows,
    and prune's "created by dev" check follows the template (old-style siblings stop matching)."""

    def setUp(self):
        super().setUp()
        self.config(f'[dev]\nrepos_dir = "{self.repos_dir}"\n'
                    '[worktree]\npath = "~/wt/{repo}/{task}"\n[branch]\ntemplate = "{user}/{task}"\n')
        self.env["FLEET_USER"] = "lau"

    def test_create_uses_templates(self):
        self.ok("platform", "fix")
        wt = os.path.join(self.home, "wt", "platform", "fix")
        self.assertEqual(self.git("-C", wt, "branch", "--show-current"), "lau/fix")
        self.assertIn(["worktree", "open", "--cwd", self.platform, "--path", wt, "--label", "fix", "--no-focus",
                       "--json"], self.herdr_calls())

    def test_prune_follows_template(self):
        add = lambda path, branch: self.git("-C", self.platform, "worktree", "add", "-q", path, "-b", branch,
                                            "origin/main")
        ours = os.path.join(self.home, "wt", "platform", "stale")
        old_style = self.platform + "-wt-legacy"
        add(ours, "lau/stale")
        add(old_style, "platform__legacy")
        r = self.ok("prune", "--dry-run")
        self.assertIn(f"prune: removing {ours}", r.stdout)
        self.assertNotIn(old_style, r.stdout)


class PrCheckout(DevTestCase):
    def test_pr_checkout(self):
        self.set_gh(view={"headRefName": "feature-x", "headRepositoryOwner": {"login": "o"},
                          "isCrossRepository": False})
        r = self.ok("prco", "7", "--repo", "platform", DEV_NO_FOCUS="1")
        wt = self.platform + "-wt-pr-7"
        self.assertEqual(r.stdout.splitlines()[-1], wt)
        self.assertEqual(self.git("-C", wt, "branch", "--show-current"), "feature-x")
        calls = self.herdr_calls()
        self.assertIn(["worktree", "open", "--cwd", self.platform, "--path", wt, "--label", "pr-7", "--no-focus",
                       "--json"], calls)
        self.assertIn(["pane", "rename", "w1:t2:p1", "platform__pr-7"], calls)

    def test_fork_refused(self):
        self.set_gh(view={"headRefName": "x", "isCrossRepository": True})
        r = self.dev("pr-checkout", "8", "--repo", "platform")
        self.assertEqual(r.returncode, 1)
        self.assertIn("PR #8 comes from a fork", r.stderr)


if __name__ == "__main__":
    unittest.main()
