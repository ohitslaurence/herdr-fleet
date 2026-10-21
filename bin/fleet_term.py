"""fleet_term — untrusted text reaching a terminal.

PR titles, branch names, notes, inbox text and agent output come from other people and from
agents. Escape sequences in them can overwrite the clipboard (OSC 52), forge links (OSC 8),
recolour or hide output. CLIs that print such text call protect_stdout() once: everything they
write then has control characters shown as visible placeholders; newlines and tabs pass.
(The curses cockpit doesn't need it: curses shows control characters as ^X itself.)
"""
import re, sys

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean(text):
    return _CTRL.sub(lambda m: "␛" if m.group() == "\x1b" else "�", text)


class _Safe:
    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        return self._stream.write(clean(text))

    def __getattr__(self, name):
        return getattr(self._stream, name)


def need_terminal(cmd, yes_hint=False, rerun=None):
    """The human's /dev/tty, or exit 2: config, keys and installs are the human's to approve.
    A guard, not a boundary: an agent with a pty can fake one (SECURITY.md)."""
    try:
        tty = open("/dev/tty", "r+")
    except OSError:
        tty = None
    if not (tty and sys.stdout.isatty()):
        how = f"run `{rerun}` in your own terminal" if rerun else "run it yourself"
        print(f"{cmd}: needs a terminal: {how}" + (", or pass --yes" if yes_hint else ""), file=sys.stderr)
        sys.exit(2)
    return tty


def confirm(cmd, question, assume_yes=False):
    """True on y/yes typed at the terminal (or --yes)."""
    if assume_yes:
        return True
    tty = need_terminal(cmd, yes_hint=True)
    tty.write(f"{question} [y/N] ")
    tty.flush()
    return tty.readline().strip().lower() in ("y", "yes")


def protect_stdout():
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if not isinstance(stream, _Safe):
            setattr(sys, name, _Safe(stream))
