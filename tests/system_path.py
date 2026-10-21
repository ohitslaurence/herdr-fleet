"""PATH for tests that run the commands with a narrowed PATH (no herdr, no gh, ...).

`/usr/bin:/bin` alone isn't enough on macOS: its python3 is Apple's 3.9 and its bash is 3.2.
So the tests' own interpreter and the first bash on the outer PATH come first, linked alone into
a folder so nothing else from their directories (gh, git, herdr) leaks in."""
import atexit, os, shutil, sys, tempfile

_dir = tempfile.mkdtemp(prefix="fleet-test-path-")
atexit.register(shutil.rmtree, _dir, True)
os.symlink(sys.executable, os.path.join(_dir, "python3"))
os.symlink(shutil.which("bash") or "/bin/bash", os.path.join(_dir, "bash"))
SYSTEM_PATH = os.pathsep.join([_dir, "/usr/bin", "/bin"])
