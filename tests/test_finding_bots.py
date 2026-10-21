"""ship.finding_bots: review bots beyond the configured provider whose findings block a merge.
Off by default (a bot nobody chose doesn't gate anyone's PRs); listing one makes it block."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bin"))
sys.path.insert(0, os.path.join(HERE, "golden"))
from facts import load_ship  # noqa: E402

S = load_ship()


def pr_with(login):
    thread = {"isResolved": False, "isOutdated": False,
              "comments": {"totalCount": 1, "nodes": [{"author": {"login": login}, "body": "SQL injection here"}]}}
    return {"reviewThreads": {"nodes": [thread]}}


def open_findings(pol, login="strix-security"):
    sev = S.open_bot_threads(pr_with(login), S.finding_bots(pol))
    return sum(v for k, v in sev.items() if k != "answered")


class FindingBots(unittest.TestCase):
    def test_an_unlisted_bot_doesnt_block(self):
        self.assertEqual(open_findings({}), 0)

    def test_a_listed_bot_blocks(self):
        self.assertEqual(open_findings({"finding_bots": ["strix"]}), 1)

    def test_the_review_bot_always_blocks(self):
        self.assertEqual(open_findings({}, login="greptile-apps"), 1)


if __name__ == "__main__":
    unittest.main()
