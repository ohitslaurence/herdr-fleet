"""Untrusted text (PR titles, notes, agent output) can't drive the terminal: escape sequences
come out as visible placeholders in every non-interactive CLI."""
import os, subprocess, sys, tempfile, unittest

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
sys.path.insert(0, BIN)
import fleet_term  # noqa: E402
from system_path import SYSTEM_PATH  # noqa: E402

OSC52 = "\x1b]52;c;UEFTU1dPUkQ=\x07"


class Clean(unittest.TestCase):
    def test_controls_become_visible(self):
        self.assertEqual(fleet_term.clean(f"title{OSC52}\tx\n\x9b2J"), "title␛]52;c;UEFTU1dPUkQ=�\tx\n�2J")

    def test_note_with_escapes_prints_safely(self):
        home = tempfile.mkdtemp()
        env = dict(os.environ, HOME=home, PATH=f"{BIN}:{SYSTEM_PATH}")
        subprocess.run([os.path.join(BIN, "fleet-cockpit"), "note", f"fix it{OSC52}"], env=env, cwd=home,
                       capture_output=True, timeout=30)
        p = subprocess.run([os.path.join(BIN, "fleet-cockpit"), "note"], env=env, cwd=home, capture_output=True,
                           text=True, timeout=30)
        self.assertNotIn("\x1b", p.stdout)
        self.assertIn("fix it␛]52", p.stdout)


if __name__ == "__main__":
    unittest.main()
