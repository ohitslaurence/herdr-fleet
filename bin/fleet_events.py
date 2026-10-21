"""fleet_events — what changed in the fleet, in order: an append-only log anyone can read back.

ship appends when a PR's state changes (ready, merged, CI red, deploy failed, ...) and the
watcher when something lands in the human's inbox. `since(ts)` returns what happened after a
moment, which is what a morning briefing needs: "since 7pm: 3 merged, 1 deploy failed".

~/.local/state/fleet/events.jsonl, one JSON object per line; rotates to .1 at MAX_BYTES.
Text in events is redacted and capped: it comes from PR titles, checks and agents.
"""
import json, os, sys
from datetime import datetime, timezone

MAX_BYTES = 2 * 1024 * 1024
TEXT_CAP = 300  # the remote API's field cap: nothing stored longer than anyone is shown


def path(state_dir):
    return os.path.join(state_dir, "events.jsonl")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append(state_dir, kind, **fields):
    """One event. A failure is reported on stderr, never raised: a lost log line must not break
    ship or the watcher."""
    try:
        from fleet_redact import redact
        from fleet_state import append_jsonl
        clean = {k: (redact(v)[:TEXT_CAP] if isinstance(v, str) else v) for k, v in fields.items()}
        append_jsonl(path(state_dir), {"at": now_iso(), "kind": kind, **clean}, MAX_BYTES)
    except Exception as e:
        print(f"fleet events: not recorded ({type(e).__name__}: {e})", file=sys.stderr)


def since(state_dir, ts, limit=500):
    """Events after `ts` (ISO 8601), oldest first, at most `limit` (the newest ones)."""
    cutoff = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    out = []
    for p in (path(state_dir) + ".1", path(state_dir)):
        try:
            with open(p) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        if datetime.fromisoformat(e["at"]) > cutoff:
                            out.append(e)
                    except (ValueError, KeyError, TypeError):
                        continue
        except FileNotFoundError:
            continue
    return out[-limit:]
