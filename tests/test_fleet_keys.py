"""Key management uses temporary homes and never calls a provider."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import fleet_keys as K
import fleet_decide as D


class Keys(unittest.TestCase):
    def setUp(self):
        home = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, HOME=home))
        self.file = Path(K.path())
        self.http = self.enterContext(patch.object(K.urllib.request, "urlopen"))
        self.http.side_effect = AssertionError("unexpected network request")

    def write(self, text, mode=0o600):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        if self.file.exists():
            self.file.chmod(0o600)
        self.file.write_text(text)
        self.file.chmod(mode)

    def run_cli(self, *args, tty=False, value=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch.object(sys.stdin, "isatty", return_value=tty), \
                patch.object(out, "isatty", return_value=tty), \
                patch.object(K.getpass, "getpass", return_value=value):
            code = K.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_missing_status_and_empty_test(self):
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"file": str(self.file), "exists": False, "mode": None,
                         "ok_mode": False, "keys": dict.fromkeys(K.KNOWN, False), "unknown": []})
        self.assertEqual(self.run_cli("test", "--json")[:2], (0, "{}\n"))
        self.assertFalse(self.file.exists())

    def test_status_names_only_and_owner_only_modes(self):
        for mode in (0o600, 0o400, 0o644, 0o700, 0o1600):
            self.write('# comment\nTYPESAFE_API_KEY="private-value"\nOPENAI_API_KEY=\nFOO=other-value\n', mode)
            code, out, err = self.run_cli("status", "--json")
            result = json.loads(out)
            self.assertEqual(code, int(bool(mode & 0o077)))  # only group/other access is wrong
            self.assertEqual(result["mode"], f"{mode:04o}")
            self.assertEqual(result["unknown"], ["FOO"])
            self.assertEqual(result["keys"], dict(zip(K.KNOWN, (True, False))))
            self.assertTrue(all(value not in out + err for value in ("private-value", "other-value")))

    def test_shared_reader_ignores_environment_and_malformed_lines(self):
        self.write('# comment\nTYPESAFE_API_KEY = "stored"\nbad line\nOPENAI_API_KEY=\nFOO=unknown\n')
        with patch.dict(os.environ, OPENAI_API_KEY="environment"):
            self.assertTrue(K.keys() == D.keys() == {"TYPESAFE_API_KEY": "stored"})
        self.file.chmod(0o644)
        with self.assertRaises(PermissionError):
            K.keys()
        self.assertEqual(self.run_cli("test", "--json")[0], 1)
        self.http.assert_not_called()

    def test_new_uses_shared_reader(self):
        loader = importlib.machinery.SourceFileLoader("fleet_new_keys_test", str(BIN / "fleet-new"))
        new = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(new)
        self.write('OPENAI_API_KEY=stored\n')
        new.load_env()
        self.assertTrue(new.KEYS == K.keys())
        self.assertNotIn("OPENAI_API_KEY", os.environ)

    def test_set_unset_require_both_terminals(self):
        for command in ("set", "unset"):
            for stdin, stdout in ((False, False), (False, True), (True, False)):
                with patch.object(sys.stdin, "isatty", return_value=stdin), \
                        patch.object(sys.stdout, "isatty", return_value=stdout), \
                        patch.object(K.getpass, "getpass") as prompt, contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(K.main([command, K.KNOWN[0]]), 2)
                    self.assertIn("needs a terminal: run it yourself", err.getvalue())
                    prompt.assert_not_called()
        self.assertFalse(self.file.exists())

    def test_edit_preserves_unrelated_lines_and_removes_duplicates(self):
        self.write('# keep\nTYPESAFE_API_KEY=old\nFOO=untouched\nTYPESAFE_API_KEY=duplicate\nOPENAI_API_KEY=other')
        code, out, err = self.run_cli("set", K.KNOWN[0], tty=True, value="  replacement  ")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertTrue(self.file.read_text() == '# keep\nFOO=untouched\nOPENAI_API_KEY=other\nTYPESAFE_API_KEY=replacement\n')
        self.assertEqual(self.file.stat().st_mode & 0o7777, 0o600)
        self.assertEqual(self.run_cli("unset", K.KNOWN[0], tty=True)[0], 0)
        self.assertNotIn(K.KNOWN[0], K.parse(self.file.read_text()))

    def test_create_and_noop_unset(self):
        self.assertEqual(self.run_cli("unset", K.KNOWN[0], tty=True)[0], 0)
        self.assertFalse(self.file.exists())
        self.assertEqual(self.run_cli("set", K.KNOWN[0], tty=True, value="private")[0], 0)
        self.assertEqual(self.file.stat().st_mode & 0o7777, 0o600)

    def test_quotes_round_trip(self):
        for value in ('"quoted"', '"', '"prefix', 'suffix"', "ordinary"):
            self.assertEqual(self.run_cli("set", K.KNOWN[0], tty=True, value=value)[0], 0)
            self.assertTrue(K.keys()[K.KNOWN[0]] == value)

    def test_invalid_or_empty_input_does_not_write_or_echo(self):
        for value, code in (("  ", 3), ("private=bad", 2), ("private\n", 2), ("private\rvalue", 2)):
            got, out, err = self.run_cli("set", K.KNOWN[0], tty=True, value=value)
            self.assertEqual(got, code)
            self.assertNotIn("private", out + err)
            self.assertFalse(self.file.exists())
        for args in (("set", "FOO"), ("unset", "FOO"), ("test", "FOO"), ("status", "extra"), (),
                     ("set", K.KNOWN[0], "--json"), ("status", "--json", "--json")):
            code, out, err = self.run_cli(*args, tty=True)
            self.assertEqual(code, 2)
            if "--json" in args:
                self.assertIn("error", json.loads(out))

    def test_atomic_failure_leaves_original_and_no_temp_file(self):
        self.write("TYPESAFE_API_KEY=original\n")
        with patch.object(K.os, "replace", side_effect=OSError("private-value")):
            code, out, err = self.run_cli("set", K.KNOWN[0], tty=True, value="private-value")
        self.assertEqual(code, 1)
        self.assertTrue(self.file.read_text() == "TYPESAFE_API_KEY=original\n")
        self.assertNotIn("private-value", out + err)
        self.assertEqual(list(self.file.parent.iterdir()), [self.file])

    def test_symlink_and_nonregular_file_refused(self):
        self.file.parent.mkdir(parents=True)
        target = self.file.parent / "target"
        target.write_text("TYPESAFE_API_KEY=private\n")
        self.file.symlink_to(target)
        self.assertEqual(self.run_cli("status", "--json")[0], 1)
        self.assertEqual(self.run_cli("set", K.KNOWN[0], tty=True, value="replacement")[0], 1)
        self.assertTrue(target.read_text() == "TYPESAFE_API_KEY=private\n")
        self.file.unlink()
        os.mkfifo(self.file)
        self.assertEqual(self.run_cli("status", "--json")[0], 1)

    def test_getpass_cannot_fall_back_to_echo(self):
        def prompt(*args):
            import warnings
            warnings.warn("echo", K.getpass.GetPassWarning)
            raise AssertionError("must stop before echo")
        out = io.StringIO()
        with patch.object(sys.stdin, "isatty", return_value=True), contextlib.redirect_stdout(out), \
                patch.object(out, "isatty", return_value=True), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(K.getpass, "getpass", side_effect=prompt):
            self.assertEqual(K.main(["set", K.KNOWN[0]]), 2)
        self.assertFalse(self.file.exists())

    def test_provider_requests_are_minimal_and_bounded(self):
        self.write("TYPESAFE_API_KEY=private-jev\nOPENAI_API_KEY=private-openai\n")
        self.http.side_effect = None
        self.http.return_value.__enter__.return_value.status = 200
        with patch.object(D.fleet_config, "get", return_value="jev-latest"):
            code, out, err = self.run_cli("test", "--json")
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(list(result), list(K.KNOWN))
        self.assertEqual(self.http.call_count, 2)
        for call in self.http.call_args_list:
            self.assertEqual(call.kwargs, {"timeout": 5})
        jev, openai = [call.args[0] for call in self.http.call_args_list]
        self.assertEqual(jev.full_url, D.ENDPOINT)
        self.assertEqual(jev.get_method(), "POST")
        self.assertEqual(len(json.loads(jev.data)["questions"]), 1)
        self.assertEqual(openai.full_url, "https://api.openai.com/v1/models")
        self.assertEqual(openai.get_method(), "GET")
        self.assertIsNone(openai.data)
        self.assertTrue(jev.get_header("Authorization") == "Bearer private-jev")
        self.assertNotIn("private", out + err)
        self.assertTrue(all(item["status"] == "ok" and isinstance(item["ms"], int) for item in result.values()))

    def test_failures_do_not_leak_or_retry(self):
        self.write("OPENAI_API_KEY=private\n")
        for error, status in ((urllib.error.HTTPError("url", 401, "private", {}, None), "rejected (401)"),
                              (urllib.error.HTTPError("url", 429, "private", {}, None), "rejected (429)"),
                              (urllib.error.URLError("private"), "unreachable"),
                              (TimeoutError("private"), "unreachable"),
                              (K.http.client.BadStatusLine("private"), "unreachable")):
            self.http.reset_mock()
            self.http.side_effect = error
            code, out, err = self.run_cli("test", "OPENAI_API_KEY", "--json")
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)["OPENAI_API_KEY"]["status"], status)
            self.assertNotIn("private", out + err)
            self.http.assert_called_once()
        code, out, _ = self.run_cli("test", "OPENAI_API_KEY")
        self.assertEqual((code, out), (1, "OPENAI_API_KEY unreachable\n"))
        self.http.reset_mock()
        self.assertEqual(self.run_cli("test", "TYPESAFE_API_KEY", "--json")[:2], (0, "{}\n"))
        self.http.assert_not_called()

    def test_bad_config_is_distinct_from_provider_failure(self):
        self.write("TYPESAFE_API_KEY=private\n")
        with patch.object(D.fleet_config, "get", side_effect=RuntimeError("private")):
            code, out, err = self.run_cli("test", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), {"error": "cannot load Fleet configuration"})
        self.assertNotIn("private", out + err)
        self.http.assert_not_called()

    def test_dispatch_propagates_exit_code(self):
        self.write("OPENAI_API_KEY=private\n", 0o644)
        result = subprocess.run([str(BIN / "fleet"), "keys", "status", "--json"],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["ok_mode"])
        self.assertNotIn("private", result.stdout + result.stderr)


class EmptyTest(unittest.TestCase):
    def test_says_when_there_is_nothing_to_test(self):
        home = tempfile.mkdtemp()
        p = subprocess.run([os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "fleet"),
                            "keys", "test"], capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, HOME=home))
        self.assertEqual(p.returncode, 0)
        self.assertIn("no keys set", p.stdout)


if __name__ == "__main__":
    unittest.main()
