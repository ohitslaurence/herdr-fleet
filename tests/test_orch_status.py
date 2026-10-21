"""JSON status extends orch without changing startup or wait behavior."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from system_path import SYSTEM_PATH  # noqa: E402

BIN = Path(__file__).resolve().parents[1] / "bin"


class Status(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.fake = self.home / "bin"
        self.fake.mkdir()
        self.state = self.home / "orch"
        self.env = dict(os.environ, HOME=str(self.home), ORCH_HOME=str(self.state),
                        PATH=f"{self.fake}:{SYSTEM_PATH}")
        herdr = self.fake / "herdr"
        herdr.write_text('#!/bin/sh\necho \'{"result":{"agent":{"agent_status":"idle"}}}\'\n')
        herdr.chmod(0o755)

    def run_orch(self, *args):
        return subprocess.run([str(BIN / "orch"), *args], capture_output=True, text=True,
                              env=self.env, timeout=10)

    def worker(self, status=""):
        directory = self.state / "worker"
        directory.mkdir(parents=True)
        (directory / "meta").write_text("name=worker\nkind=codex\n")
        (directory / "status").write_text(status)
        return directory

    def test_json_both_positions_and_empty_registry(self):
        p = self.run_orch("--json", "status")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), {"workers": []})
        directory = self.worker("working: starting\ndone: shipped\n")
        (directory / "report.md").write_text("report")
        for args in (("--json", "status"), ("status", "--json")):
            p = self.run_orch(*args)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(json.loads(p.stdout), {"workers": [{"name": "worker", "kind": "codex",
                "herdr": "idle", "state": "done", "status": "done: shipped", "turn_minutes": None, "report": True}]})

    def test_text_uses_same_worker_data(self):
        directory = self.worker("working: checking\n")
        (directory / "report.md").write_text("report")
        data = json.loads(self.run_orch("--json", "status").stdout)["workers"][0]
        p = self.run_orch("status")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.splitlines()[1].split(), [data["name"], data["kind"], data["herdr"],
                                                          data["state"], "-", "yes"])
        self.assertEqual(p.stdout.splitlines()[2].strip(), data["status"])

    def test_unreachable_herdr_keeps_local_worker_status(self):
        self.worker("blocked: input needed\n")
        (self.fake / "herdr").write_text("#!/bin/sh\nexit 1\n")
        p = self.run_orch("status", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        row = json.loads(p.stdout)["workers"][0]
        self.assertEqual((row["herdr"], row["state"]), ("-", "blocked"))

    def test_invalid_status_flags_are_usage_errors(self):
        for args in (("--json", "wait"), ("status", "--bogus"), ("--json", "status", "stray")):
            p = self.run_orch(*args)
            self.assertEqual(p.returncode, 2, p.stderr)
            self.assertIn("usage:", p.stderr)


if __name__ == "__main__":
    unittest.main()
