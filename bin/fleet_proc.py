"""fleet_proc — run a command with a timeout, turning a hang into an ordinary failure.

The fleet's long-running process runs ship's GitHub calls in-process; one `gh` that never
returns used to stop the whole watch loop. Every gh/git/herdr call goes through run(): a timeout
comes back as returncode 124 with the reason on stderr, which callers already treat as a failure.
"""
import json, subprocess

TIMEOUT_S = 90


def run(args, timeout=None, **kw):
    """timeout: seconds, default [agents] command_timeout_s (90)."""
    if timeout is None:
        try:
            import fleet_config
            timeout = fleet_config.get("agents", "command_timeout_s", TIMEOUT_S)
        except Exception:  # config unreadable: still never hang
            timeout = TIMEOUT_S
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    try:
        return subprocess.run(args, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"{args[0]}: timed out after {timeout} s")


def gh_text(*args, timeout=None):
    """`gh <args>`'s stdout, or None if it failed or timed out."""
    p = run(["gh", *args], timeout=timeout)
    return p.stdout if p.returncode == 0 else None


def gh_json(*args, timeout=None):
    """`gh <args>`'s output parsed, or None if it failed, timed out or wasn't JSON."""
    out = gh_text(*args, timeout=timeout)
    try:
        return json.loads(out) if out and out.strip() else None
    except ValueError:
        return None


def herdr(*args, timeout=10):
    """Run a herdr command; HerdrDown (with herdr's own reason) if it can't."""
    try:
        p = run(["herdr", *args], timeout=timeout)
    except FileNotFoundError:
        raise HerdrDown("herdr isn't on PATH") from None
    if p.returncode:
        raise HerdrDown((p.stderr.strip().splitlines() or [f"`herdr {' '.join(args)}` exited {p.returncode}"])[0][:200])
    return p.stdout


class HerdrDown(RuntimeError):
    """herdr couldn't be asked. Not the same as "no agents": callers must not read it as one."""


def herdr_agents():
    """herdr's agent panes. Raises HerdrDown with the reason when herdr can't answer."""
    try:
        p = run(["herdr", "agent", "list"], timeout=10)  # a local socket: slower than this is down
    except FileNotFoundError:
        raise HerdrDown("herdr isn't on PATH")
    if p.returncode:
        raise HerdrDown((p.stderr.strip().splitlines() or [f"`herdr agent list` exited {p.returncode}"])[0][:200])
    try:
        r = json.loads(p.stdout)["result"]
        return r["agents"] if isinstance(r, dict) else r
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise HerdrDown(f"unexpected answer from `herdr agent list`: {p.stdout.strip()[:80]!r}") from e
