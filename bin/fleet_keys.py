"""fleet keys — manage API keys without displaying their values.

  fleet keys status [--json]
  fleet keys set <NAME>              prompt privately in a terminal
  fleet keys unset <NAME>            remove a key in a terminal
  fleet keys test [NAME] [--json]     one request per stored key, 5 s timeout

Known keys: TYPESAFE_API_KEY (Jev), OPENAI_API_KEY (OpenAI).
File: ~/.config/fleet/env, mode 0600. Keys never enter child environments.
Exit: 0 success, 1 wrong existing mode or failed probe, 2 usage, 3 empty input.
"""
import getpass, http.client, json, os, re, secrets, stat, sys, time, urllib.error, urllib.request, warnings

KNOWN = ("TYPESAFE_API_KEY", "OPENAI_API_KEY")


def path():
    return os.path.expanduser("~/.config/fleet/env")


def parse(text):
    values = {}
    for line in text.splitlines():
        name, sep, value = line.strip().partition("=")
        name = name.strip()
        if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            value = value.strip()
            if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            values[name] = value
    return values


def owner_only(mode):
    """0600, or stricter (0400): what matters is that no one else can read it."""
    return mode is not None and not mode & 0o077


def read(file=None, *, require_mode=True):
    """Read through one descriptor so permissions apply to the file actually read."""
    file = file if file is not None else path()
    try:
        fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return "", None
    with os.fdopen(fd) as stream:
        mode = os.fstat(stream.fileno()).st_mode
        if not stat.S_ISREG(mode):
            raise OSError("keys file must be a regular file")
        if require_mode and not owner_only(stat.S_IMODE(mode)):
            raise PermissionError("keys file is readable by others: chmod 600 ~/.config/fleet/env")
        return stream.read(), stat.S_IMODE(mode)


def keys(file=None):
    text, _ = read(file)
    return {name: value for name, value in parse(text).items() if name in KNOWN and value}


def status():
    text, mode = read(require_mode=False)
    values = parse(text)
    return {"file": str(path()), "exists": mode is not None,
            "mode": f"{mode:04o}" if mode is not None else None,
            "ok_mode": owner_only(mode),
            "keys": {name: bool(values.get(name)) for name in KNOWN},
            "unknown": sorted(name for name in values if name not in KNOWN)}


def edit(name, value):
    file = path()
    text, mode = read(require_mode=False)
    if value is None and mode is None:
        return
    lines = text.splitlines(keepends=True)
    kept = [line for line in lines if name not in parse(line)]
    if value is not None:
        if kept and not kept[-1].endswith("\n"):
            kept[-1] += "\n"
        if value.startswith('"') and value.endswith('"'):
            value = f'"{value}"'
        kept.append(f"{name}={value}\n")
    parent = os.path.dirname(file)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    temporary = os.path.join(parent, f".env-{secrets.token_hex(12)}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)  # a restrictive umask must still produce 0600
            stream.write("".join(kept))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, file)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def probe(name, value):
    if name == "TYPESAFE_API_KEY":
        from fleet_decide import jev_request
        questions = {"check": {"type": "choice", "instructions": "Choose yes.",
                               "criteria": {"yes": None, "no": None}}}
        request = jev_request(value, "Choose yes.", questions)
    else:
        request = urllib.request.Request("https://api.openai.com/v1/models",
                                         headers={"Authorization": f"Bearer {value}"})
    start = time.monotonic()
    result = "unreachable"
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            result = "ok" if 200 <= response.status < 300 else "unreachable"
    except urllib.error.HTTPError as error:
        result = f"rejected ({error.code})"
        error.close()
    except (OSError, ValueError, http.client.HTTPException):
        pass  # provider errors may contain the credential
    return {"status": result, "ms": int((time.monotonic() - start) * 1000)}


def main(argv):
    if any(arg in ("-h", "--help", "help") for arg in argv):
        print(__doc__.strip())
        return 0
    as_json = "--json" in argv

    def fail(message, code):
        if as_json:
            print(json.dumps({"error": message}))
        else:
            print(f"fleet keys: {message}", file=sys.stderr)
        return code

    args = [arg for arg in argv if arg != "--json"]
    if argv.count("--json") > 1 or not args:
        return fail("usage: fleet keys status|set|unset|test (see --help)", 2)
    command, *rest = args
    counts = {"status": (0,), "test": (0, 1), "set": (1,), "unset": (1,)}
    valid = len(rest) in counts.get(command, ())
    if rest and rest[0] not in KNOWN:
        valid = False
    if as_json and command in ("set", "unset"):
        valid = False
    if not valid:
        return fail("usage: fleet keys status|set|unset|test; known names: " + ", ".join(KNOWN), 2)
    try:
        if command == "status":
            result = status()
            if as_json:
                print(json.dumps(result))
            else:
                print(f"{result['file']}: " + (f"mode {result['mode']}" if result['exists'] else "missing"))
                for name, present in result["keys"].items():
                    print(f"{name}: {'present' if present else 'missing'}")
                if result["unknown"]:
                    print("unknown: " + ", ".join(result["unknown"]))
            return int(result["exists"] and not result["ok_mode"])
        if command == "test":
            present = keys()
            result = {name: probe(name, present[name]) for name in (rest or KNOWN) if name in present}
            if as_json:
                print(json.dumps(result))
            elif not result:
                print("no keys set: nothing to test (`fleet keys set <NAME>` adds one)")
            else:
                for name, outcome in result.items():
                    print(f"{name} {outcome['status']}")
            return int(any(outcome["status"] != "ok" for outcome in result.values()))
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return fail("needs a terminal: run it yourself", 2)
        value = None
        if command == "set":
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass(f"{rest[0]}: ")
            if any(char in value for char in "=\r\n"):
                return fail("key must not contain '=' or a newline", 2)
            value = value.strip()
            if not value:
                return 3
        edit(rest[0], value)
        return 0
    except (EOFError, KeyboardInterrupt):
        return fail("cancelled", 1)
    except getpass.GetPassWarning:
        return fail("cannot read a key without echo", 2)
    except RuntimeError:
        return fail("cannot load Fleet configuration", 1)
    except (OSError, UnicodeError):
        return fail("cannot access keys file; use a regular file with mode 0600", 1)
