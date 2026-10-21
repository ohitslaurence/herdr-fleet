"""fleet_serve: the local socket (allowlisted ops with declared args, owner-only, bounded, one
server per state dir) and the herdr event subscriber (wakes on status changes, resubscribes when
the pane set changes, survives a hostile stream). Plus the hardening around it from the red-team:
the watch's Refresher, timeouts, and the cockpit's in-process ship."""
import importlib.machinery, importlib.util, json, os, socket, stat, sys, tempfile, threading, time, unittest

from unittest.mock import Mock, patch

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
sys.path.insert(0, BIN)
import fleet_proc  # noqa: E402
import fleet_serve as F  # noqa: E402


def short_dir():
    # AF_UNIX paths max out near 108 bytes; keep test sockets short
    return tempfile.mkdtemp(dir="/tmp", prefix="fs")


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, os.path.join(BIN, path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


class PeerCredentials(unittest.TestCase):
    def test_darwin_socket_authenticates_peer_uid(self):
        import ctypes
        def getpeereid(fd, uid, gid):
            self.assertEqual(fd, 9)
            uid._obj.value = 123
            gid._obj.value = 456
            return 0
        libc = Mock()
        libc.getpeereid.side_effect = getpeereid
        connection = Mock()
        connection.fileno.return_value = 9
        with patch.object(sys, "platform", "darwin"), patch.object(ctypes, "CDLL", return_value=libc):
            self.assertEqual(F.peer_uid(connection), 123)
            libc.getpeereid.side_effect = None
            libc.getpeereid.return_value = -1
            with self.assertRaises(OSError):
                F.peer_uid(connection)
        connection.getsockopt.assert_not_called()


class Socket(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(short_dir(), "serve.sock")
        self.slow = threading.Event()

        def boom():
            raise ValueError("secret detail /home/x")

        def wait():
            self.slow.wait(5)
            return "late"
        self.srv = F.serve({"ping": lambda: {"up": True}, "echo": lambda x=None: x, "boom": boom, "wait": wait},
                           self.path)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.srv.lock_file.close)
        self.addCleanup(self.slow.set)

    def raw(self, data, read=True):
        s = socket.socket(socket.AF_UNIX)
        s.connect(self.path)
        try:
            s.sendall(data)
            if not read:
                return s
            with s.makefile("rb") as f:
                return json.loads(f.readline() or b"null")
        finally:
            if read:
                s.close()

    def call(self, op, **kw):
        return F.call(op, path=self.path, **kw)

    def test_ops_and_declared_args(self):
        self.assertEqual(self.call("ping"), {"up": True})
        self.assertEqual(self.call("echo", x=[1, 2]), [1, 2])
        self.assertEqual(self.raw(b'{"op": "ping", "id": "c-1"}\n')["result"], {"up": True})  # id tolerated

    def test_errors_have_codes(self):
        self.assertEqual(self.raw(b'{"op": "rm_rf"}\n')["error"]["code"], "unknown_op")
        self.assertIn("ping", self.raw(b'{"op": "rm_rf"}\n')["error"]["message"])
        self.assertEqual(self.raw(b"not json\n")["error"]["code"], "bad_request")
        self.assertEqual(self.raw(b"[1]\n")["error"]["code"], "bad_request")
        self.assertEqual(self.raw(b'{"op": "ping", "force": true}\n')["error"]["code"], "bad_args")
        with self.assertRaises(F.ServerError) as e:
            self.call("rm_rf")
        self.assertEqual(e.exception.code, "unknown_op")

    def test_failing_op_hides_details_and_server_survives(self):
        err = self.raw(b'{"op": "boom"}\n')["error"]
        self.assertEqual(err["code"], "failed")
        self.assertNotIn("secret", err["message"])
        self.assertEqual(self.call("ping"), {"up": True})

    def test_oversized_or_unterminated_request_is_refused(self):
        big = b'{"op": "ping"}' + b" " * (F.MAX_REQUEST + 10) + b"\n"
        self.assertEqual(self.raw(big)["error"]["code"], "bad_request")

    def test_silent_client_is_dropped(self):
        self.assertTrue(F._Handler.timeout and F._Handler.timeout <= 30, "handlers need a read timeout")
        real, F._Handler.timeout = F._Handler.timeout, 0.3
        self.addCleanup(setattr, F._Handler, "timeout", real)
        s = self.raw(b'{"op":', read=False)
        s.settimeout(3)
        self.assertEqual(s.recv(100), b"")  # the server hung up instead of waiting forever
        s.close()

    def test_too_many_in_flight_is_busy(self):
        real, self.srv.slots = self.srv.slots, threading.BoundedSemaphore(1)
        self.addCleanup(setattr, self.srv, "slots", real)
        held = self.raw(b'{"op": "wait"}\n', read=False)
        time.sleep(0.2)
        self.assertEqual(self.raw(b'{"op": "ping"}\n')["error"]["code"], "busy")
        self.slow.set()
        held.close()

    def test_slow_ops_cant_starve_ping_or_each_other(self):
        path = os.path.join(short_dir(), "pools.sock")
        with patch.dict(F.SLOW_OPS, {"wait": 2, "quick": 1}, clear=True):
            srv = F.serve({"ping": lambda: {"up": True}, "wait": lambda: self.slow.wait(5), "quick": lambda: "fast"},
                          path)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.lock_file.close)
        held = [self.raw_to(path, b'{"op": "wait"}\n') for _ in range(2)]
        time.sleep(0.3)
        self.assertEqual(self.raw_to(path, b'{"op": "wait"}\n', read=True)["error"]["code"], "busy")  # its pool
        self.assertEqual(F.call("quick", path=path), "fast")  # another slow op: its own pool, untouched
        self.assertEqual(F.call("ping", path=path), {"up": True})  # and the general pool
        self.slow.set()
        for h in held:
            h.close()

    def raw_to(self, path, data, read=False):
        real, self.path = self.path, path
        try:
            return self.raw(data, read=read)
        finally:
            self.path = real

    def test_an_op_refusal_reaches_the_client_as_its_code(self):
        path = os.path.join(short_dir(), "refuse.sock")

        def refuse():
            raise F.OpError("budget", "spent for the hour")
        srv = F.serve({"refuse": refuse}, path)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.lock_file.close)
        with self.assertRaises(F.ServerError) as cm:
            F.call("refuse", path=path)
        self.assertEqual((cm.exception.code, cm.exception.message), ("budget", "spent for the hour"))

    def test_owner_only(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode), 0o700)

    def test_second_server_refuses_even_if_the_first_is_slow(self):
        threading.Thread(target=lambda: self.raw(b'{"op": "wait"}\n'), daemon=True).start()
        with self.assertRaises(RuntimeError):
            F.serve({"ping": lambda: 1}, self.path)
        self.assertEqual(self.call("ping"), {"up": True})  # the first one still owns the socket

    def test_client_outcomes(self):
        self.assertIsNone(F.call("ping", path=os.path.join(short_dir(), "none.sock")))  # no server
        with self.assertRaises(TimeoutError):
            F.call("wait", timeout=0.3, path=self.path)
        self.assertIsNone(F.request("rm_rf", path=self.path))  # request(): any failure -> None


class StaleSocket(unittest.TestCase):
    def test_left_over_socket_is_replaced_under_the_lock(self):
        path = os.path.join(short_dir(), "serve.sock")
        s = socket.socket(socket.AF_UNIX)
        s.bind(path)  # a dead server's file
        s.close()
        srv = F.serve({"ping": lambda: "new"}, path)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.lock_file.close)
        self.assertEqual(F.call("ping", path=path), "new")


class FakeHerdr:
    """Accepts subscriptions; per connection sends an ack, then the scripted lines, then closes."""

    def __init__(self, scripts):
        self.path = os.path.join(short_dir(), "herdr.sock")
        self.scripts, self.requests = list(scripts), []
        self.s = socket.socket(socket.AF_UNIX)
        self.s.bind(self.path)
        self.s.listen(5)
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            c, _ = self.s.accept()
            f = c.makefile("rb")
            self.requests.append(json.loads(f.readline()))
            c.sendall(b'{"id":"fleet-sub","result":{"type":"subscription_started"}}\n')
            for line in (self.scripts.pop(0) if self.scripts else []):
                c.sendall(line if isinstance(line, bytes) else (json.dumps(line) + "\n").encode())
            if not self.scripts:
                time.sleep(5)  # last connection: stay open
            c.close()


class Events(unittest.TestCase):
    def start(self, herdr, panes):
        os.environ["HERDR_SOCKET_PATH"] = herdr.path
        self.addCleanup(os.environ.pop, "HERDR_SOCKET_PATH", None)
        real, F.HerdrEvents.MIN_GAP_S = F.HerdrEvents.MIN_GAP_S, 0
        self.addCleanup(setattr, F.HerdrEvents, "MIN_GAP_S", real)
        seen = []
        F.HerdrEvents(lambda: panes.pop(0) if len(panes) > 1 else panes[0],
                      lambda p, st: seen.append((p, st)), log=lambda m: None).start()
        return seen

    def wait_for(self, herdr, n):
        deadline = time.time() + 5
        while len(herdr.requests) < n and time.time() < deadline:
            time.sleep(0.05)

    def test_status_change_wakes_and_pane_changes_resubscribe(self):
        herdr = FakeHerdr([
            [{"event": "pane.agent_status_changed", "data": {"pane_id": "w1:p1", "workspace_id": "w1",
                                                             "agent_status": "idle"}},
             # a hook re-announcing a pane we already watch: wake, but no resubscribe
             {"event": "pane_agent_detected", "data": {"pane_id": "w1:p1", "agent": "claude"}},
             {"event": "pane_created", "data": {"pane": {"pane_id": "w2:p1"}}}],  # herdr's lifecycle spelling
            [],
        ])
        seen = self.start(herdr, [["w1:p1"], ["w1:p1", "w2:p1"]])
        self.wait_for(herdr, 2)
        self.assertIn(("w1:p1", "idle"), seen)
        self.assertIn(("w1:p1", None), seen)
        self.assertEqual(len(herdr.requests), 2, "pane.created must trigger a resubscribe")
        subs = herdr.requests[1]["params"]["subscriptions"]
        self.assertIn({"type": "pane.agent_status_changed", "pane_id": "w2:p1"}, subs)

    def test_oversized_event_line_reconnects_instead_of_buffering(self):
        real, F.MAX_EVENT_LINE = F.MAX_EVENT_LINE, 1000
        self.addCleanup(setattr, F, "MAX_EVENT_LINE", real)
        herdr = FakeHerdr([[b"x" * 5000 + b"\n"], []])
        self.start(herdr, [["w1:p1"]])
        self.wait_for(herdr, 2)
        self.assertEqual(len(herdr.requests), 2)


class Timeouts(unittest.TestCase):
    def test_proc_run_turns_a_hang_into_a_failure(self):
        t = time.time()
        p = fleet_proc.run(["sleep", "5"], timeout=0.3)
        self.assertEqual(p.returncode, 124)
        self.assertLess(time.time() - t, 2)

    def test_watch_run_gives_up(self):
        W = load("fw_timeouts", "fleet-watch")
        t = time.time()
        self.assertIsNone(W.run("sleep", "5", timeout=0.3))
        self.assertLess(time.time() - t, 2)


class FakeCockpit:
    def __init__(self, delay=0.3, fail=False):
        self.delay, self.fail, self.refreshes = delay, fail, 0

    def refresh(self, quiet, inprocess):
        self.refreshes += 1
        time.sleep(self.delay)
        if self.fail:
            raise KeyError("baseRefName")

    def collect(self):
        return {"rows": [], "n": self.refreshes}


class RefresherTests(unittest.TestCase):
    def setUp(self):
        self.W = load("fw_refresher", "fleet-watch")

    def test_concurrent_callers_share_one_refresh(self):
        c = FakeCockpit()
        r = self.W.Refresher(c, {}, lambda m: None)
        out = []
        ts = [threading.Thread(target=lambda: out.append(r.run())) for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(c.refreshes, 1)
        self.assertEqual(len(out), 5)
        r.run()  # finished moments ago: reused, not re-run
        self.assertEqual(c.refreshes, 1)

    def test_background_run_doesnt_block(self):
        c = FakeCockpit(delay=1)
        r = self.W.Refresher(c, {}, lambda m: None)
        t = time.time()
        self.assertIsNone(r.run(wait=False))
        self.assertLess(time.time() - t, 0.2)

    def test_failure_is_reported_not_swallowed(self):
        r = self.W.Refresher(FakeCockpit(fail=True), {}, lambda m: None)
        with self.assertRaises(RuntimeError):
            r.run()

    def test_failed_refresh_cools_down(self):
        c = FakeCockpit(delay=0, fail=True)
        r = self.W.Refresher(c, {}, lambda m: None)
        for _ in range(5):
            with self.assertRaises(RuntimeError):
                r.run()
        self.assertIsNone(r.run(wait=False))
        self.assertEqual(c.refreshes, 1)  # one attempt, then the failure answers for FRESH_S


class InProcessShip(unittest.TestCase):
    def setUp(self):
        self.C = load("fc_inproc", "fleet-cockpit")

    def test_bad_urls_and_crashes_cost_one_pr(self):
        self.assertIsNone(self.C.ship_inprocess("https://github.com/o/r/pull/12abc"))
        self.assertIsNone(self.C.ship_inprocess("https://ghe.example.com/o/r/pull/1"))

        class Boom:
            def evaluate(self, *a):
                raise KeyError("baseRefName")
        self.C._SHIP["m"] = Boom()
        self.assertIsNone(self.C.ship_inprocess("https://github.com/o/r/pull/1"))

    def test_merge_confirmation_names_what_is_merged(self):
        r = {"where": "herdr-fleet#14", "repo": "evil/repo", "number": 9, "head": "abcdef123456"}
        self.assertEqual(self.C.target(r), "evil/repo#9 @ abcdef1")
        ok, _ = self.C.merge(dict(r, head=None))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
