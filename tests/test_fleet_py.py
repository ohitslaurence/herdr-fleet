"""An older python3 (macOS ships 3.9) gets one clear line from every command, not a traceback."""
import os, subprocess, sys, unittest

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
OLD = ("import sys; sys.version_info = (3, 9, 6, 'final', 0); sys.version = '3.9.6 (fake)'; "
       f"sys.path.insert(0, {BIN!r}); import fleet_config")


class OldPython(unittest.TestCase):
    def test_says_what_to_do(self):
        p = subprocess.run([sys.executable, "-c", OLD], capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 1)
        self.assertIn("needs Python 3.11 or newer, but `python3` here is 3.9.6", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
