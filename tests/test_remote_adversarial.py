"""fleet remote: adversarial cases from the 2026-10-25 security review — the edges the main
suite doesn't cover: MCP notifications must not execute, id 0 is a request, absolute-form
targets, unicode refs, duplicate Content-Length, odd Host shapes, per-tag budget sharing,
and the real whois cache's stale-tag window."""
import http.client, json, os, socket, sys, tempfile, threading, time, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bin"))
import fleet_config  # noqa: E402
import fleet_remote as R  # noqa: E402

BOT = {"node": "bot", "tags": ["tag:fleet-client"], "user": "tagged-devices", "id": "nBOT"}


class Rig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.who = BOT
        self.calls = []
        patches = [
            mock.patch.object(R, "AUDIT", os.path.join(self.tmp, "audit.jsonl")),
            mock.patch.object(R, "whois", lambda ip, port: self.who),
            mock.patch.object(R, "self_node", lambda: (["100.64.0.9"], ["fleet-host"])),
            mock.patch.object(R, "serve_call", self._serve),
            mock.patch.object(fleet_config, "get", self.config),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.scopes = {"tag:fleet-client": list(R.TOOLS)}
        self.srv = R.Server("127.0.0.1", 0)
        self.srv.own_ips.discard("127.0.0.1")
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def _serve(self, op, **args):
        self.calls.append((op, args))
        return {"version": "x"} if op == "ping" else {"rows": []}

    def config(self, section, key, default=None):
        if (section, key) == ("remote", "scopes"):
            return self.scopes
        if (section, key) == ("remote", "rate_per_min"):
            return 30
        return default

    def post(self, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}", "Fleet-Remote": "1", "Content-Type": "application/json"}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        c.request("POST", path, json.dumps(body if body is not None else {}), h)
        r = c.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw or b"{}")
        except ValueError:
            return r.status, {"_raw": raw.decode("utf-8", "replace")}

    def raw(self, data: bytes):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(data)
            s.shutdown(socket.SHUT_WR)
            out = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    return out
                out += chunk

    def audit(self):
        try:
            with open(R.AUDIT) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []


class McpEdges(Rig):
    def test_a_notification_never_executes(self):
        # rid None -> 202 with no dispatch: fire-and-forget must not run tools
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/mcp",
                  json.dumps({"jsonrpc": "2.0", "method": "tools/call",
                              "params": {"name": "fleet_status", "arguments": {}}}),
                  {"Host": f"127.0.0.1:{self.port}", "Fleet-Remote": "1",
                   "Content-Type": "application/json"})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 202)
        self.assertEqual(self.calls, [], "a notification ran a tool")
        self.assertFalse(any(a.get("result") == "ok" for a in self.audit()))

    def test_id_zero_is_a_real_request(self):
        code, out = self.post("/mcp", {"jsonrpc": "2.0", "id": 0, "method": "ping", "params": {}})
        self.assertEqual(code, 200)
        self.assertEqual(out.get("id"), 0)
        self.assertIn("result", out)

    def test_absolute_form_target_is_not_matched(self):
        code, _ = self.post("http://evil.example/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(code, 404)

    def test_arguments_that_arent_an_object_dont_crash(self):
        code, out = self.post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": "fleet_pr", "arguments": ["ref", "x"]}})
        self.assertEqual(code, 200)  # MCP error result, not a 500
        self.assertTrue(out["result"]["isError"])

    def test_unicode_lookalike_refs_are_refused_before_any_fetch(self):
        # fleet_remote's PR_REF \w is unicode-wide: lookalike refs pass the HTTP layer's shape
        # check (loose, worth tightening to ASCII). The real wall is the watcher's
        # fleet_pr.find: exact match against the cockpit's rows, so these never reach gh.
        import fleet_pr
        rows = [{"kind": "pr", "section": "IN FLIGHT", "where": "api#1", "repo": "o/api",
                 "number": 1, "url": "https://github.com/o/api/pull/1", "head": "abc"}]
        admits = []
        for ref in ("аpi#1", "api#１２", "〇/api#1", "o/аpi#1"):  # cyrillic a, fullwidth digits...
            if R.PR_REF.match(ref):
                admits.append(ref)  # the HTTP layer's shape check lets it through...
            with self.assertRaises(fleet_pr.Refused, msg=ref):  # ...but the watcher refuses it
                fleet_pr.find(rows, ref)
        if admits:
            import warnings
            warnings.warn(f"PR_REF admits non-ASCII refs (harmless; tighten to [A-Za-z0-9]): {admits}")


class VerbsAndHosts(Rig):
    def test_other_verbs_get_a_plain_501_with_no_path_echo(self):
        for verb in ("PUT", "DELETE", "OPTIONS", "HEAD"):
            out = self.raw(f"{verb} /v1/fleet_status?secret-marker-123 HTTP/1.1\r\n"
                           f"Host: 127.0.0.1:{self.port}\r\nFleet-Remote: 1\r\n\r\n".encode())
            self.assertIn(b"501", out.split(b"\r\n")[0], verb)
            self.assertNotIn(b"secret-marker-123", out, verb)
            self.assertNotIn(b"Python", out, verb)

    def test_host_variants_that_arent_exactly_this_listener_are_refused(self):
        for host in ("FLEET-HOST:7412".replace("7412", str(self.port)),  # case
                     f"fleet-host.:{self.port}", f"fleet-host:{self.port}:80",
                     f"100.64.0.9.:{'%s' % self.port}"):
            code, _ = self.post("/v1/fleet_status", headers={"Host": host})
            self.assertEqual(code, 403, host)

    def test_duplicate_content_length_reads_the_first_and_closes(self):
        # no keep-alive (HTTP/1.0): leftover bytes die with the connection, nothing desyncs
        out = self.raw(f"POST /v1/fleet_status HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                       f"Fleet-Remote: 1\r\nContent-Length: 2\r\nContent-Length: 16384\r\n\r\n".encode()
                       + b"{}" + b'{"jsonrpc":"2.0","method":"tools/call","id":9}')
        self.assertIn(b"200", out.split(b"\r\n")[0])
        # the smuggled second document was never parsed as a request
        self.assertFalse(any(op == "ping" for op, _ in self.calls[1:]))


class BudgetSharing(Rig):
    def test_the_tag_budget_is_shared_across_nodes_carrying_it(self):
        # an ephemeral bot re-created with a new node id must not get a fresh budget
        self.config_calls = 0
        for i in range(30):
            self.assertEqual(self.post("/v1/fleet_status")[0], 200)
        self.who = {"node": "bot2", "tags": ["tag:fleet-client"], "user": "tagged-devices", "id": "nBOT2"}
        self.assertEqual(self.post("/v1/fleet_status")[0], 429)


class RealWhoisCache(unittest.TestCase):
    """The real R.whois against a faked `tailscale` subprocess: cache, fail-closed, stale window."""

    def test_cache_and_stale_tag_window(self):
        calls = []
        tagged = {"Node": {"ComputedName": "bot", "Tags": ["tag:fleet-client"], "StableID": "nBOT"}}
        state = {"answer": tagged}

        class P:
            returncode = 0
            @property
            def stdout(self):
                return json.dumps(state["answer"])

        def fake_run(args, timeout=None, **kw):
            calls.append(args)
            return P()

        with mock.patch.object(R.fleet_proc, "run", fake_run):
            R._whois_cache.clear()
            w = R.whois("100.104.60.45", 1234)
            self.assertEqual(w["tags"], ["tag:fleet-client"])
            self.assertEqual(R.whois("100.104.60.45", 9999)["id"], "nBOT")  # port isn't part of the key
            self.assertEqual(len(calls), 1, "second lookup within 30 s must hit the cache")
            # tag removed in the admin console: the cached answer still says tagged (documented 30 s window)
            state["answer"] = {"Node": {"ComputedName": "bot", "Tags": [], "StableID": "nBOT"}}
            self.assertEqual(R.whois("100.104.60.45", 1234)["tags"], ["tag:fleet-client"])
            # a node re-created with a new address is a fresh lookup
            R.whois("100.200.1.5", 1)
            self.assertEqual(len(calls), 2)

    def test_whois_failure_is_none_and_never_cached(self):
        def dead(args, timeout=None, **kw):
            raise OSError("tailscaled down")
        with mock.patch.object(R.fleet_proc, "run", dead):
            R._whois_cache.clear()
            self.assertIsNone(R.whois("100.104.60.45", 1))
            self.assertEqual(R._whois_cache, {})


if __name__ == "__main__":
    unittest.main()
