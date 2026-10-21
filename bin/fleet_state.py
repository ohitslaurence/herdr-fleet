"""fleet_state — locked, atomic JSON files shared by the fleet tools.

Several processes write the same small JSON files (notes.json: fleet note, fleet new and its
background polish; watch.json: the watch daemon and `fleet inbox done`). A plain
read-modify-write loses updates, and a shared `.tmp` path can corrupt the file. Every writer
goes through update(): an flock on a sidecar lock file, a fresh read, the change, then a
unique temp file + os.replace, so readers never see a half-written file.

    from fleet_state import update, read
    update(NOTES, lambda notes: notes.update({path: {...}}))
"""
import fcntl, json, os, tempfile
from contextlib import contextmanager


def read(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {} if default is None else default
    except json.JSONDecodeError:
        # never silently treat a corrupt file as empty: that's how every note gets lost
        raise RuntimeError(f"{path} is not valid JSON; fix or remove it")


@contextmanager
def locked(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".lock", "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield


def write(path, data):
    """Atomic replace via a unique temp file in the same directory. Caller holds the lock."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def append_jsonl(path, obj, max_bytes, keep=1):
    """One line onto an owner-only log, rotating past max_bytes to <path>.1 .. <path>.<keep> (the
    oldest dropped). One write per line, so concurrent appenders don't interleave."""
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    try:
        if os.path.getsize(path) > max_bytes:
            for n in range(keep - 1, 0, -1):
                if os.path.exists(f"{path}.{n}"):
                    os.replace(f"{path}.{n}", f"{path}.{n + 1}")
            os.replace(path, path + ".1")
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(obj) + "\n")


def update(path, fn, default=None):
    """Lock, re-read, apply fn(data) (mutate in place or return a new value), write back."""
    with locked(path):
        data = read(path, default)
        out = fn(data)
        data = data if out is None else out
        write(path, data)
        return data
