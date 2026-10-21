"""`ship pr` builds one GraphQL request from refs and search text the caller supplies (agents
copy them from PR comments). Nothing they type may change the request's shape."""
import json, os, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_ship_gates import load_ship  # noqa: E402

PAYLOAD = 'https://github.com/x"}},viewer{login,repositories(first:50){nodes{name}}},z:{repository(owner:"a/b/pull/1'


class ShipPrInjection(unittest.TestCase):
    def setUp(self):
        self.S = load_ship()
        self.calls = []

        def fake(args, **kw):
            self.calls.append(args)
            return type("P", (), {"returncode": 0, "stdout": json.dumps({"data": {}}), "stderr": ""})()
        patch = mock.patch.object(self.S.proc, "run", fake)  # fleet_proc is shared by every test module
        patch.start()
        self.addCleanup(patch.stop)
        self.S.search_scope = lambda: "org:acme"
        self.S.default_owner = lambda: "acme"

    def query(self):
        args = self.calls[0]
        return args[args.index("-f") + 1], args

    def test_crafted_url_is_not_a_ref_and_never_enters_the_query(self):
        self.assertIsNone(self.S.pr_ref(PAYLOAD))
        self.S.cmd_pr([PAYLOAD, "acme/app#12"])
        q, args = self.query()
        self.assertNotIn("viewer", q)
        self.assertNotIn(PAYLOAD, q)
        self.assertIn(f"q0=org:acme is:pr {PAYLOAD} sort:updated-desc", args)  # a variable, verbatim
        self.assertIn("o0=acme", args)
        self.assertIn("n0=12", args)

    def test_query_text_is_fixed_shape(self):
        self.S.cmd_pr(['"){evil}', "app#3"])
        q, _ = self.query()
        self.assertEqual(q.count("{"), q.count("}"))
        self.assertNotIn("evil", q)

    def test_names_must_look_like_github_names(self):
        self.assertIsNone(self.S.pr_ref("https://github.com/a/../b/pull/1"))
        self.assertEqual(self.S.pr_ref("https://github.com/acme/app/pull/7/files"), ("acme", "app", 7))


if __name__ == "__main__":
    unittest.main()
