"""fleet new starts any configured coding agent: the default from [roles.driver], another when
the sentence names it, permission bypass only when the config opts in."""
import contextlib, importlib.machinery, importlib.util, io, os, subprocess, sys, tempfile, textwrap, unittest
from unittest import mock

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
sys.path.insert(0, BIN)
import fleet_config  # noqa: E402
from system_path import SYSTEM_PATH  # noqa: E402

loader = importlib.machinery.SourceFileLoader("fleet_new_t", os.path.join(BIN, "fleet-new"))
spec = importlib.util.spec_from_loader("fleet_new_t", loader)
N = importlib.util.module_from_spec(spec)
loader.exec_module(N)


class Agents(unittest.TestCase):
    def configure(self, text=""):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "config.toml")
        with open(path, "w") as f:
            f.write(textwrap.dedent(text))
        real = fleet_config.PATH
        fleet_config.PATH = path
        fleet_config._cache.clear()
        self.addCleanup(lambda: (setattr(fleet_config, "PATH", real), fleet_config._cache.clear()))
        # never run a harness's real model-list command (or write its cache) in tests
        self.listed = {}
        real_models = N.harness_models
        N.harness_models = lambda kind: list(fleet_config.agent_kinds()[kind].get("models", [])) + self.listed.get(kind, [])
        self.addCleanup(setattr, N, "harness_models", real_models)

    def test_built_ins_and_safe_default(self):
        self.configure()
        self.assertEqual(set(fleet_config.agent_kinds()), {"claude", "codex", "opencode"})
        self.assertEqual(N.default_agent(), "claude")
        self.assertEqual(N.agent_argv("claude"), ["claude"])
        self.assertEqual(N.agent_argv("opencode"), ["opencode"])

    def test_bypass_and_models_are_opt_in(self):
        self.configure("""
            [agents]
            permissions = "bypass"
            [roles.driver]
            kind = "opencode"
            model = "opencode/kimi-k3"
            [kinds.codex]
            model = "gpt-6"
            """)
        self.assertEqual(N.default_agent(), "opencode")
        self.assertEqual(N.agent_argv("opencode"), ["opencode", "--model", "opencode/kimi-k3", "--auto"])
        self.assertEqual(N.agent_argv("codex"), ["codex", "--model", "gpt-6", "--dangerously-bypass-approvals-and-sandbox"])

    def test_default_is_configurable_and_jev_is_told(self):
        self.configure('[roles.driver]\nkind = "codex"\n')
        q = N.agent_questions()[0]["agent"]["criteria"]
        self.assertIn("The default", q["codex"])
        self.assertNotIn("The default", q["claude"])

    def test_no_model_words_are_built_in(self):
        self.configure()
        for kind, spec in fleet_config.agent_kinds().items():
            for word in ("kimi", "gpt", "sonnet", "opus", "openai", "anthropic"):
                self.assertNotIn(word, spec.get("names", []), (kind, word))
        self.assertIsNone(N.named_harness("platform, use kimi to fix the flaky test"))

    def test_harness_is_named_by_its_own_names(self):
        self.configure()
        self.assertEqual(N.named_harness("api, do it in opencode"), "opencode")
        self.assertEqual(N.named_harness("api, codex please"), "codex")
        self.assertIsNone(N.named_harness("api, fix the login bug"))

    def test_nickname_picks_harness_and_model(self):
        self.configure('[models.kimi]\nharness = "opencode"\nmodel = "opencode/kimi-k3"\n')
        self.assertEqual(N.resolve_agent("platform, use kimi to fix it")[:2], ("opencode", "opencode/kimi-k3"))
        self.assertEqual(N.resolve_agent("platform, fix it")[:2], ("claude", None))  # default harness+model

    def test_model_from_a_harness_list_and_a_named_harness_wins(self):
        self.configure()
        self.listed["opencode"] = ["openai/gpt-6-sol", "opencode/kimi-k3"]
        crit, picks = N.model_options()
        key = next(k for k, v in picks.items() if v == ("opencode", "openai/gpt-6-sol"))
        # "gpt sol but in opencode": Jev picks the model; the named harness agrees
        self.assertEqual(N.resolve_agent("api, use gpt sol in opencode", "opencode", key, picks),
                         ("opencode", "openai/gpt-6-sol", None))
        # a harness that doesn't list the model: still that harness, with a warning
        harness, model, warn = N.resolve_agent("api, use gpt sol in claude", "claude", key, picks)
        self.assertEqual((harness, model), ("claude", "openai/gpt-6-sol"))
        self.assertIn("doesn't list", warn)

    def test_no_version_named_means_newest_of_the_family(self):
        self.configure()
        self.listed["opencode"] = ["opencode/gpt-5.6-sol", "opencode/gpt-6.1-sol", "openai/gpt-6-sol-fast",
                                   "openai/gpt-6.1-sol-fast"]
        crit, picks = N.model_options()
        old = next(k for k, v in picks.items() if v == ("opencode", "opencode/gpt-5.6-sol"))
        fast = next(k for k, v in picks.items() if v == ("opencode", "openai/gpt-6-sol-fast"))
        self.assertEqual(N.resolve_agent("api, gpt sol in opencode", "opencode", old, picks)[1], "opencode/gpt-6.1-sol")
        self.assertEqual(N.resolve_agent("api, gpt sol fast", "opencode", fast, picks)[1], "openai/gpt-6.1-sol-fast")
        self.assertEqual(N.resolve_agent("api, gpt 5.6 sol", "opencode", old, picks)[1], "opencode/gpt-5.6-sol")

    def test_custom_harness(self):
        self.configure("""
            [kinds.gemini]
            cmd = ["gemini"]
            bypass = ["--yolo"]
            about = "Gemini CLI"
            names = ["gemini"]
            [agents]
            permissions = "bypass"
            """)
        self.assertEqual(N.named_harness("web, tidy the css with gemini"), "gemini")
        self.assertEqual(N.agent_argv("gemini"), ["gemini", "--yolo"])
        self.assertEqual(N.agent_argv("gemini", "gemini-3-pro"), ["gemini", "--model", "gemini-3-pro", "--yolo"])

    def test_fleet_role_accepts_any_configured_agent(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".config", "fleet"))
        with open(os.path.join(home, ".config", "fleet", "config.toml"), "w") as f:
            f.write('[kinds.gemini]\ncmd = ["gemini"]\n[roles.worker]\nkind = "gemini"\n')
        p = subprocess.run([os.path.join(BIN, "fleet"), "role", "worker"], capture_output=True, text=True,
                           env=dict(os.environ, HOME=home), timeout=30)
        self.assertEqual(p.stdout.split()[0], "gemini")


class Focus(unittest.TestCase):
    """After starting a session, you land on its agent tab, even before herdr has noticed the
    agent (it only knows one once the CLI is up)."""

    def focus(self, tab_id, fail=None):
        def herdr(*args):
            if args[:2] == fail:
                raise N.fleet_proc.HerdrDown("no such workspace")
            return ""
        with mock.patch.object(N.fleet_proc, "herdr", side_effect=herdr) as h:
            N.focus_session("w7", tab_id)
        return [c.args for c in h.call_args_list]

    def test_workspace_then_agent_tab(self):
        self.assertEqual(self.focus("w7:t2"), [("workspace", "focus", "w7"), ("tab", "focus", "w7:t2")])

    def test_no_agent_tab_still_shows_the_workspace(self):
        self.assertEqual(self.focus(None), [("workspace", "focus", "w7")])

    def test_a_failure_is_said_not_swallowed(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.focus("w7:t2", fail=("workspace", "focus")), [("workspace", "focus", "w7")])
        self.assertIn("couldn't switch to it (no such workspace)", err.getvalue())


class FirstInstruction(unittest.TestCase):
    """The sentence names the work; it reaches the agent only when asked (`p`, --prompt, config)."""

    def preview(self, *flags, config=""):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, "code", "api", ".git"))
        os.makedirs(os.path.join(home, ".config", "fleet"))
        with open(os.path.join(home, ".config", "fleet", "config.toml"), "w") as f:
            f.write(f'[dev]\nrepos_dir = "{home}/code"\n' + config)
        p = subprocess.run([os.path.join(BIN, "fleet-new"), "--dry-run", *flags, "api, fix the flaky login test"],
                           capture_output=True, text=True, timeout=60, cwd=home,
                           env=dict(os.environ, HOME=home, PATH=f"{BIN}:{SYSTEM_PATH}", FLEET_CLASSIFIER="haiku"))
        return next(line for line in p.stdout.splitlines() if line.strip().startswith("agent"))

    def test_preview_says_which(self):
        self.assertIn("waiting for your first instruction", self.preview())
        self.assertIn('starting on: "api, fix the flaky login test"', self.preview("--prompt"))
        self.assertIn("starting on:", self.preview(config="[new]\nsend_sentence = true\n"))
        self.assertIn("waiting for", self.preview("--no-prompt", config="[new]\nsend_sentence = true\n"))

    def test_what_reaches_the_agent(self):
        idle = N.launch_cmd("claude", None, "fix it", send=False, installing=False)
        self.assertNotIn("fix it", idle)
        self.assertEqual(N.launch_cmd("claude", None, "fix it", send=True, installing=False)[-1], "fix it")
        idle_installing = N.launch_cmd("claude", None, "fix it", send=False, installing=True)
        self.assertEqual(idle_installing[-2:], ["--append-system-prompt", N.SETUP_NOTE])  # still told, not prompted
        no_flag = N.launch_cmd("codex", None, "fix it", send=True, installing=True)
        self.assertIn(N.SETUP_NOTE, no_flag[-1])  # without a system flag the note rides on the instruction
        self.assertNotIn(N.SETUP_NOTE, " ".join(N.launch_cmd("codex", None, "fix it", send=False, installing=True)))


if __name__ == "__main__":
    unittest.main()
