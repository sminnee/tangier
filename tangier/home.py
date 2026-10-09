"""`~/.tangier/`: the machine-wide gate jobs and their config. `TANGIER_HOME` moves it.

See `docs/specs/gate.md#jobs`.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass

from tangier.gate import GateError

DEFAULT_MAX_RUNNING = 4


@dataclass(frozen=True)
class HomeConfig:
    # How many jobs run at once on this machine. The rest queue.
    max_running: int = DEFAULT_MAX_RUNNING


def root() -> str:
    return os.environ.get("TANGIER_HOME") or os.path.join(os.path.expanduser("~"), ".tangier")


def config_path() -> str:
    return os.path.join(root(), "config.toml")


def load_config() -> HomeConfig:
    """Read `config.toml`. No file gives the defaults. An unknown key or a bad value is an error."""
    path = config_path()
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return HomeConfig()
    except tomllib.TOMLDecodeError as e:
        raise GateError(f"{tilde(path)}: {e}") from e
    unknown = sorted(set(data) - {"jobs"})
    if unknown:
        raise GateError(f"{tilde(path)}: unknown table or key: {', '.join(unknown)}")
    jobs = data.get("jobs", {})
    if not isinstance(jobs, dict):
        raise GateError(f"{tilde(path)}: `jobs` must be a table")
    unknown = sorted(set(jobs) - {"max-running"})
    if unknown:
        raise GateError(f"{tilde(path)}: unknown key in [jobs]: {', '.join(unknown)}")
    max_running = jobs.get("max-running", DEFAULT_MAX_RUNNING)
    if not isinstance(max_running, int) or isinstance(max_running, bool) or max_running < 1:
        raise GateError(f"{tilde(path)}: [jobs] max-running must be a whole number of 1 or more, not {max_running!r}")
    return HomeConfig(max_running=max_running)


def tilde(path: str) -> str:
    """`path` with the home directory as `~`."""
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def makedirs(path: str) -> None:
    """Create `path`. When it cannot be written, as in a sandbox, say what to allow."""
    try:
        os.makedirs(path, exist_ok=True)
    except PermissionError as e:
        raise unwritable(path, e) from e


def unwritable(path: str, e: OSError) -> GateError:
    """The error for a path under `root()` that cannot be written, as in a sandbox that allows only the repo."""
    return GateError(
        f"cannot write {path}: {e.strerror}. Gate jobs live in {root()}; "
        "in a sandbox, add it to the write allowlist (or set TANGIER_HOME)"
    )
