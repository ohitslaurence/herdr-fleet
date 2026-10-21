"""Imported before tomllib by every module that needs it: on an older `python3` (Apple's own is
3.9 on macOS) every command then says what's wrong in one line instead of a traceback.
Keep this file parseable by old Pythons."""
import sys

if sys.version_info < (3, 11):
    raise SystemExit(f"herdr-fleet needs Python 3.11 or newer, but `python3` here is "
                     f"{sys.version.split()[0]} ({sys.executable}). Put a newer python3 first on PATH "
                     "(macOS: `brew install python`).")
