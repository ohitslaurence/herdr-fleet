"""ship's free review must never write or read through files the PR controls (C1, H2)."""
import importlib.machinery, importlib.util, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

BIN = os.path.join(os.path.dirname(__file__), "..", "bin")


def load_ship(state_dir):
    loader = importlib.machinery.SourceFileLoader("ship_under_test", os.path.join(BIN, "ship"))
    spec = importlib.util.spec_from_loader("ship_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod.STATE_DIR = state_dir
    return mod


class FreeReviewSymlinks(unittest.TestCase):
    def test_pr_symlinks_cannot_redirect_writes_or_reads(self):
        tmp = tempfile.mkdtemp()
        victim = os.path.join(tmp, "victim-bashrc")
        secret = os.path.join(tmp, "victim-env")
        Path(victim).write_text("original\n")
        Path(secret).write_text("TYPESAFE_API_KEY=jev_" + "Zq8Wv2Xk5Ym1Tn4Rp7Ls0Hd3\n")
        # the "PR": a repo whose head commits symlinks named like ship's files
        repo = os.path.join(tmp, "repo")
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
        for name, target in ((".fleet-pr.md", victim), (".fleet-pr.diff", victim), (".fleet-review-out", secret)):
            os.symlink(target, os.path.join(repo, name))
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "evil"], check=True)
        head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

        S = load_ship(os.path.join(tmp, "state"))
        S.evaluate = lambda o, r, n: ({"repo": "o/r", "number": 1}, {"headRefOid": head, "baseRefName": "main",
                                                                     "title": "PWNED_TITLE", "url": "u"}, None)
        S.reviewer_for = lambda s: ("claude", ("codex", "-", "-"))
        S.repo_path = lambda r: repo
        real_run = S.run

        def fake_run(*args, check=True):
            if args[:2] == ("git", "-C") and "fetch" in args:
                return ""
            if args[0] == "gh" and "diff" in args:
                return "PWNED_DIFF"
            if args[0] == "gh" and "view" in args:
                return "PWNED_BODY"
            if args[0] == "gh":
                raise AssertionError(f"unexpected gh call {args}")
            return real_run(*args, check=check)
        S.run = fake_run
        seen = {}

        def fake_headless(kind, model, effort, prompt, cwd, io_dir):
            seen["prompt"], seen["cwd"], seen["io"] = prompt, cwd, io_dir
            return "nothing\nVERDICT: clean"
        S.headless = fake_headless
        S.free_review("o/r", 1, post=False)

        self.assertEqual(Path(victim).read_text(), "original\n", "PR symlink redirected ship's write")
        self.assertNotIn(os.path.realpath(seen["cwd"]), os.path.realpath(seen["io"]))
        self.assertIn(seen["io"], seen["prompt"])
        self.assertFalse(os.path.exists(seen["io"]), "io dir should be removed after the review")

    def test_reviewer_has_no_shell(self):
        S = load_ship(tempfile.mkdtemp())
        calls = []
        fake = lambda args, **kw: calls.append(args) or type("P", (), {"stdout": "", "returncode": 0})()  # noqa: E731
        with mock.patch.object(S.subprocess, "run", fake):  # the module is shared: never leave it patched
            S.headless("claude", "-", "-", "p", "/tmp", tempfile.mkdtemp())
        tools = calls[0][calls[0].index("--allowedTools") + 1:]
        self.assertFalse([t for t in tools if t.startswith("Bash")], tools)


if __name__ == "__main__":
    unittest.main()
