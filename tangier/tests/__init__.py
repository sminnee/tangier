import atexit
import os
import shutil
import subprocess
import sys
import tempfile

# A push or fetch can start `git gc --auto` in the background. It writes into
# `objects/pack` while a test's temporary repo is being removed, and the cleanup
# then fails with "Directory not empty". Every git the tests run inherits this.
os.environ.update(
    {
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "gc.auto",
        "GIT_CONFIG_VALUE_0": "0",
        "GIT_CONFIG_KEY_1": "receive.autogc",
        "GIT_CONFIG_VALUE_1": "false",
        "GIT_CONFIG_KEY_2": "maintenance.auto",
        "GIT_CONFIG_VALUE_2": "false",
    }
)


def _bypass_xcrun_git_shim() -> None:
    """Put the real git first on PATH, alone in its own dir.

    On macOS, /usr/bin/git is an xcrun shim that locates Xcode before it execs
    the real git. That costs about 8ms a call, and the suite makes about 9k
    calls. Only git goes on PATH: Xcode's other tools (python3 among them)
    would shadow the user's own. If xcrun fails, PATH is left alone.
    """
    if sys.platform != "darwin" or shutil.which("git") != "/usr/bin/git":
        return
    try:
        found = subprocess.run(["xcrun", "-f", "git"], capture_output=True, text=True)
    except OSError:
        return
    real_git = found.stdout.strip()
    if found.returncode != 0 or not real_git:
        return
    git_dir = tempfile.mkdtemp(prefix="tangier-git-")
    atexit.register(shutil.rmtree, git_dir, ignore_errors=True)
    os.symlink(real_git, os.path.join(git_dir, "git"))
    os.environ["PATH"] = git_dir + os.pathsep + os.environ.get("PATH", "")


_bypass_xcrun_git_shim()
