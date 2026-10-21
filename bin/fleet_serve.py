"""fleet_serve — the long-running half of the fleet: a local socket that answers from memory, and
a herdr event stream that wakes the watch loop when an agent changes state.

`fleet watch` (the systemd unit / watch pane) runs both. Clients (cockpit, `fleet ls`) call
request() and fall back to computing the answer themselves only when nothing is listening, so the
server is never a hard dependency.

Socket: ~/.local/state/fleet/serve.sock, mode 0600 in a 0700 directory, and every connection's
peer uid must be ours (SO_PEERCRED / getpeereid). One request per connection, one JSON object per line:
    {"op": "ping"} -> {"ok": true, "result": {...}}
    errors: {"ok": false, "error": {"code": "bad_request|unknown_op|bad_args|busy|failed", "message": ...}}
Operations are a fixed allowlist registered by the server (see fleet-watch); each declares its
arguments, and nothing here runs commands or reads files on a client's behalf.

Limits: one server per state dir (an flock held for the process lifetime), 10 s to send a
request, 64 KiB per request line, 32 requests in flight; more get `busy`.
"""
import fcntl, inspect, json, os, socket, socketserver, struct, sys, threading, time

STATE_DIR = os.path.expanduser("~/.local/state/fleet")
SOCK = os.path.join(STATE_DIR, "serve.sock")
MAX_REQUEST = 64 * 1024
READ_TIMEOUT_S = 10
MAX_INFLIGHT = 32
# Slow ops wait in a small pool of their own, one per op: they can't starve ping, or each other
# (a remote caller's pr fetches can't keep your own refresh waiting).
SLOW_OPS = {"refresh": 4, "pr": 2}
MAX_EVENT_LINE = 1024 * 1024


class OpError(Exception):
    """An op's own refusal, passed to the client as its code and message (not logged as a crash)."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ServerError(Exception):
    """The server answered, with an error. Not a reason to fall back and redo its work locally."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


def call(op, timeout=2.0, path=None, **args):
    """-> result. None when no server is running (connect failed). Raises ServerError when one
    answered with an error, and TimeoutError when it didn't answer in time."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path or SOCK)
    except OSError:
        s.close()
        return None
    try:
        s.sendall((json.dumps({"op": op, **args}) + "\n").encode())
        with s.makefile("rb") as f:
            line = f.readline(64 * 1024 * 1024)
    except socket.timeout:
        raise TimeoutError(f"fleet server didn't answer {op!r} within {timeout:.0f} s")
    except OSError:
        return None
    finally:
        s.close()
    try:
        reply = json.loads(line or b"null")
    except ValueError:
        raise ServerError("bad_reply", "unreadable reply")
    if not isinstance(reply, dict):
        raise ServerError("bad_reply", "empty reply")
    if not reply.get("ok"):
        err = reply.get("error") or {}
        if isinstance(err, str):
            err = {"code": "failed", "message": err}
        raise ServerError(err.get("code", "failed"), err.get("message", ""))
    return reply.get("result")


def request(op, timeout=2.0, path=None, **args):
    """The server's answer, or None for any failure (no server, error, timeout). For callers that
    only want a fast path and will compute the answer themselves otherwise."""
    try:
        return call(op, timeout, path, **args)
    except (ServerError, TimeoutError):
        return None


def _params(fn):
    sig = inspect.signature(fn)
    return {n for n, p in sig.parameters.items() if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}


def peer_uid(connection):
    if sys.platform == "darwin":
        import ctypes
        # Darwin has getpeereid rather than Linux's SO_PEERCRED.
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        return uid.value
    creds = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


class _Handler(socketserver.StreamRequestHandler):
    timeout = READ_TIMEOUT_S

    def handle(self):
        if peer_uid(self.request) != os.getuid():
            return  # another user on the box: say nothing
        self.slots = self.server.slots  # release exactly the pool we took from
        if not self.slots.acquire(blocking=False):
            return self._send({"ok": False, "error": {"code": "busy", "message": "too many requests in flight"}})
        self.holding = True
        try:
            self._serve_one()
        finally:
            if self.holding:
                self.slots.release()

    def _serve_one(self):
        try:
            line = self.rfile.readline(MAX_REQUEST + 1)
        except (OSError, socket.timeout):
            return
        if not line:
            return
        if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
            return self._error("bad_request", f"one JSON object per line, at most {MAX_REQUEST} bytes")
        try:
            req = json.loads(line)
        except ValueError:
            return self._error("bad_request", "not JSON")
        if not isinstance(req, dict) or not isinstance(req.get("op"), str):
            return self._error("bad_request", 'expected {"op": "<name>", ...}')
        fn = self.server.ops.get(req["op"])
        if fn is None:
            return self._error("unknown_op", f"ops: {', '.join(sorted(self.server.ops))}")
        args = {k: v for k, v in req.items() if k not in ("op", "id")}
        extra = set(args) - self.server.params[req["op"]]
        if extra:
            return self._error("bad_args", f"{req['op']} takes {sorted(self.server.params[req['op']]) or 'no arguments'}")
        pool = self.server.slow_pools.get(req["op"])
        if pool:
            if not pool.acquire(blocking=False):
                return self._error("busy", f"{req['op']} has as many callers waiting as it allows")
            self.slots.release()  # wait in the slow pool, not in the general one
            self.holding = False
        try:
            self._send({"ok": True, "result": fn(**args)})
        except OpError as e:
            self._error(e.code, str(e))
        except Exception as e:  # one bad request must not take the server down; details stay in the log
            print(f"fleet serve: {req['op']} failed: {type(e).__name__}: {e}"[:300], file=sys.stderr)
            self._error("failed", f"{req['op']} failed; see the fleet watch log")
        finally:
            if pool:
                pool.release()

    def _error(self, code, message):
        self._send({"ok": False, "error": {"code": code, "message": message}})

    def _send(self, obj):
        try:
            self.wfile.write((json.dumps(obj, default=str) + "\n").encode())
        except OSError:
            pass  # the client went away: nothing to tell it


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        print(f"fleet serve: connection error: {sys.exc_info()[1]!r}"[:200], file=sys.stderr)


def single_instance(path=SOCK):
    """Take the one-watcher lock (<path>.lock) for this process's lifetime, or raise. Two watchers
    would both answer agents and both send merge notices. Returns the open lock file: keep it."""
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    lock = open(path + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError(f"another fleet watch is running (it holds {path}.lock); not starting a second one")
    return lock


def serve(ops, path=SOCK, lock=None):
    """Start the socket server on a daemon thread. ops: {name: callable(**declared args)}.
    One server per socket path: pass the lock from single_instance(), or serve() takes it. A
    second server raises instead of taking the socket over."""
    lock = lock or single_instance(path)
    if os.path.lexists(path):
        os.remove(path)  # we hold the lock, so whatever is there is left over from a dead server
    old = os.umask(0o077)
    try:
        srv = _Server(path, _Handler)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    srv.ops = dict(ops)
    srv.params = {name: _params(fn) for name, fn in srv.ops.items()}
    srv.slots = threading.BoundedSemaphore(MAX_INFLIGHT)
    srv.slow_pools = {op: threading.BoundedSemaphore(n) for op, n in SLOW_OPS.items()}
    srv.lock_file = lock  # keeps the flock for the server's lifetime
    threading.Thread(target=srv.serve_forever, daemon=True, name="fleet-serve").start()
    return srv


# ── herdr events ─────────────────────────────────────────────────────────────

def herdr_socket():
    return os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")


class HerdrEvents:
    """Subscribes to agent status changes on every agent pane, plus pane create/close (which
    change the set, so we resubscribe). on_change(pane_id, status) runs on this thread: keep it
    cheap (set a flag). Reconnects with backoff; resubscribes at most once per MIN_GAP_S; on
    `events_lost` it resubscribes, and the watch's next full pass reconciles whatever was missed."""

    LIFECYCLE = ("pane.created", "pane.closed", "pane.agent_detected")
    MIN_GAP_S = 2

    def __init__(self, list_panes, on_change, log=print):
        self.list_panes, self.on_change, self.log = list_panes, on_change, log
        self.connected = self.was_connected = False
        self.events = 0

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="herdr-events").start()
        return self

    def _run(self):
        backoff, last = 1, 0.0
        while True:
            time.sleep(max(0.0, self.MIN_GAP_S - (time.time() - last)))
            last = time.time()
            ok = False
            try:
                self._subscribe_once()
                ok = True
            except Exception as e:  # herdr restarting, socket gone, protocol error: retry
                self.log(f"herdr events: {type(e).__name__}: {e}"[:200])
            if ok or self.was_connected:
                backoff = 1  # a healthy stream that ended isn't a reason to slow down
            else:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _subscribe_once(self):
        self.was_connected = False
        panes = sorted(self.list_panes())
        known = set(panes)
        subs = [{"type": t} for t in self.LIFECYCLE]
        subs += [{"type": "pane.agent_status_changed", "pane_id": p} for p in panes]
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(herdr_socket())
        try:
            s.sendall((json.dumps({"id": "fleet-sub", "method": "events.subscribe",
                                   "params": {"subscriptions": subs}}) + "\n").encode())
            f = s.makefile("rb")
            first = json.loads(f.readline(MAX_EVENT_LINE) or b"null")
            if not first or "error" in first:
                raise RuntimeError(f"subscribe refused: {(first or {}).get('error')}")
            self.connected = self.was_connected = True
            while True:
                line = f.readline(MAX_EVENT_LINE + 1)
                if not line:
                    raise RuntimeError("herdr closed the event stream")
                if len(line) > MAX_EVENT_LINE:
                    raise RuntimeError("event line too long; reconnecting")
                msg = json.loads(line)
                if "error" in msg:  # events_lost or similar: resubscribe from scratch
                    raise RuntimeError(f"subscription ended: {msg['error']}")
                kind, data = msg.get("event") or "", msg.get("data") or {}
                if "." not in kind:  # lifecycle events arrive as pane_created, status ones as pane.x
                    kind = kind.replace("_", ".", 1)
                if not data.get("pane_id") and isinstance(data.get("pane"), dict):
                    data = dict(data, pane_id=data["pane"].get("pane_id"))
                self.events += 1
                if kind == "pane.agent_status_changed":
                    self.on_change(data.get("pane_id"), data.get("agent_status"))
                elif kind in self.LIFECYCLE:
                    pane = data.get("pane_id")
                    self.on_change(pane, None)
                    # resubscribe only when the set changed (agent hooks re-announce known panes)
                    if (kind == "pane.closed") == (pane in known):
                        return
        finally:
            self.connected = False
            s.close()


if __name__ == "__main__":
    op = sys.argv[1] if len(sys.argv) > 1 else "ping"
    if any(a in ("-h", "--help", "help") for a in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    try:
        got = call(op)
    except (ServerError, TimeoutError) as e:
        sys.exit(f"fleet server: {e}")
    if got is None:
        sys.exit("fleet server: not running (nothing listening on " + SOCK + ")")
    print(json.dumps(got, indent=1))
