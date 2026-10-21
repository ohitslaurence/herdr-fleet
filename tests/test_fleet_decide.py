"""Decision layer: modes, fail-open, logging without state text, and secret redaction.
Run: python3 -m unittest discover -s tests   (no network: Jev is stubbed)"""
import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
import fleet_decide as F  # noqa: E402

# Fake credentials built at runtime so the repo never holds a scannable key.
from test_fleet_redact import FAKE  # noqa: E402  (secrets that must never reach Jev)


class Decide(unittest.TestCase):
    def restore_config(self):
        F.fleet_config.PATH = self.saved
        F.fleet_config._cache.clear()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        F.LOG = os.path.join(self.tmp, "d.jsonl")
        # the real loader, pointed at a temp config
        C = F.fleet_config
        self.saved = C.PATH
        C.PATH = os.path.join(self.tmp, "config.toml")
        C._cache.clear()
        with open(C.PATH, "w") as f:
            f.write('[decisions]\n"t.act" = "act"\n"t.off" = "off"\n')
        self.addCleanup(self.restore_config)
        self.sent = []

        def fake_post(state, questions, timeout=1.5):
            self.sent.append(F.redact(state))
            return {"k": {"type": "choice", "choice": "a", "probabilities": {"a": 0.9, "b": 0.1}, "confidence": 0.8},
                    "y": {"type": "noul", "noul": 0.7}}
        self.real_jev, F.jev = F.jev, fake_post
        self.Q = {"k": {"type": "choice", "instructions": "pick", "criteria": {"a": None, "b": None}}}

    def tearDown(self):
        F.jev = self.real_jev

    def log(self):
        return [json.loads(line) for line in Path(F.LOG).read_text().splitlines()]

    def test_modes(self):
        self.assertFalse(F.decide("t.shadow", "x", "w", "s", self.Q).act)
        d = F.decide("t.act", "x", "w", "s", self.Q)
        self.assertTrue(d.act)
        self.assertEqual(d.choice("k"), ("a", 0.9))
        self.assertEqual(d.yes("y"), 0.7)
        n = len(self.sent)
        d = F.decide("t.off", "x", "w", "s", self.Q)
        self.assertEqual(len(self.sent), n)
        self.assertFalse(d.act)

    def test_fails_open(self):
        def boom(*a, **k):
            raise TimeoutError("slow")
        F.jev = boom
        d = F.decide("t.act", "x", "w", "s", self.Q)
        self.assertIsNone(d.answers)
        self.assertFalse(d.act)
        self.assertIn("slow", self.log()[-1]["error"])

    def test_log_has_no_state_text(self):
        secret_state = {"agent_last_output": f"here is {FAKE['openai']} and private notes"}
        F.decide("t.act", "x", "w", secret_state, self.Q, rule="inbox", propose=lambda a: "auto-reply")
        raw = Path(F.LOG).read_text()
        self.assertNotIn("private notes", raw)
        self.assertNotIn(FAKE["openai"], raw)
        e = self.log()[-1]
        self.assertNotIn("state_preview", e)
        self.assertEqual((e["rule"], e["proposal"]), ("inbox", "auto-reply"))

    def test_debug_preview_is_redacted(self):
        with open(F.fleet_config.PATH, "a") as f:
            f.write("debug = true\n")
        F.fleet_config._cache.clear()
        F.decide("t.act", "x", "w", f"tail {FAKE['github']}", self.Q)
        e = self.log()[-1]
        self.assertIn("[REDACTED:github-token]", e["state_preview"])

    def test_jev_redacts_before_sending(self):
        # the real jev() redacts first thing; check by intercepting the HTTP layer
        captured = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"answers": {}}'

        def fake_urlopen(req, timeout=0):
            captured["body"] = req.data.decode()
            return Resp()
        F.jev = self.real_jev
        real_urlopen, F.urllib.request.urlopen = F.urllib.request.urlopen, fake_urlopen
        # keys() reads only the 0600 keys file, never the environment
        try:
            with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, HOME=home):
                path = os.path.join(home, ".config/fleet/env")
                os.makedirs(os.path.dirname(path))
                fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
                with os.fdopen(fd, "w") as stream:
                    stream.write("TYPESAFE_API_KEY=test-key\n")
                F.jev({"t": f"leak {FAKE['aws']}"}, self.Q)
        finally:
            F.urllib.request.urlopen = real_urlopen
        self.assertNotIn(FAKE["aws"], captured["body"])
        self.assertIn("REDACTED", captured["body"])


if __name__ == "__main__":
    unittest.main()
