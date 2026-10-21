"""`t` in the cockpit: when fleet new takes you to a session the cockpit closes, as a jump does
(a popup cockpit left open hides behind it and herdr refuses every other popup); when you
cancel, you stay. fleet new says which by its exit code."""
import importlib.machinery, importlib.util, os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_fleet_gh import load_cockpit  # noqa: E402

C = load_cockpit()
BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
loader = importlib.machinery.SourceFileLoader("fleet_new_c", os.path.join(BIN, "fleet-new"))
N = importlib.util.module_from_spec(importlib.util.spec_from_loader("fleet_new_c", loader))
loader.exec_module(N)


class AfterNewSession(unittest.TestCase):
    def test_started_closes_cancelled_stays_failed_pauses(self):
        self.assertEqual(C.after_new_session(0), "close")
        self.assertEqual(C.after_new_session(N.CANCELLED), "stay")
        self.assertEqual(C.after_new_session(1), "error")
        self.assertEqual(C.after_new_session(-15), "error")  # killed

    def test_cancelling_at_a_prompt_says_cancelled(self):
        from unittest import mock
        with mock.patch("builtins.input", side_effect=EOFError), mock.patch("builtins.print"):
            with self.assertRaises(SystemExit) as e:
                N.ask("› ")
        self.assertEqual(e.exception.code, N.CANCELLED)


if __name__ == "__main__":
    unittest.main()
