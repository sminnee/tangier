"""Gate jobs: a `gate run` that outlives the shell that started it. See `docs/specs/gate.md#jobs`.

A job is a directory under `<git-dir>/tangier/jobs/<id>/`:

  job.json    what ran: id, pid, argv, gates, base, the commit and tree, dirty, started
  gates.json  each gate's state, start and end, exit code, key, and where its output starts
  output.log  the job's combined stdout and stderr
  alive       locked for as long as the job process lives
  done        written last, holding the job's exit code

The job process writes `gates.json` through a `Reporter`. Every other command
only reads the directory, apart from `cancel` and `prune`.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import signal
import sys
from collections.abc import Generator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from tangier import gate, git
from tangier.config import Config
from tangier.runner import Runner

# A gate state that will not change again.
FINISHED = ("passed", "failed", "not-needed", "verified", "unrecorded", "error", "cancelled", "died")
# The finished states that count as a pass.
OK = ("passed", "not-needed", "verified")

# The automatic prune: finished jobs past KEEP_FOR, then the oldest while the logs pass
# MAX_LOG_BYTES. The newest KEEP_LATEST always stay.
KEEP_LATEST = 5
KEEP_FOR = timedelta(hours=24)
MAX_LOG_BYTES = 50 * 1024 * 1024

# How long `cancel` waits after SIGTERM before it sends SIGKILL.
CANCEL_GRACE = 10
CANCEL_POLL = 0.5

# The exit code a cancelled job records in `done`, as a shell does for SIGINT.
CANCELLED_CODE = 130


class JobError(gate.GateError):
    """A job cannot be started or found."""


@dataclass
class GateState:
    name: str
    # The key the gate runs against: from the job's snapshot until the gate starts, then its own.
    key: str
    state: str = "pending"
    code: int | None = None
    started: str | None = None
    ended: str | None = None
    # Where the gate's output starts in `output.log`, and where it ends once the gate has finished.
    offset: int | None = None
    end: int | None = None
    # The result line the run printed, as ``gate `lint`: verified (...)``.
    line: str = ""
    # The record a pass wrote.
    ref: str | None = None

    @property
    def finished(self) -> bool:
        return self.state in FINISHED


@dataclass
class Job:
    dir: str
    id: int
    pid: int
    argv: list[str]
    base: str
    head: str
    tree: str
    dirty: bool
    started: str
    gates: list[GateState] = field(default_factory=list)
    # The exit code from `done`, or None while the job has not finished.
    code: int | None = None
    # `running`, `died`, `cancelled`, `passed` or `failed`, as `load` read it.
    state: str = "running"

    @property
    def log(self) -> str:
        return os.path.join(self.dir, "output.log")

    @property
    def commit_label(self) -> str:
        """`a1b2c3d`, or `a1b2c3d+dirty` when the tree held uncommitted work."""
        return self.head[:7] + ("+dirty" if self.dirty else "")

    def gate(self, name: str) -> GateState:
        return next(g for g in self.gates if g.name == name)

    def current(self) -> GateState | None:
        """The running gate, if one is."""
        return next((g for g in self.gates if g.state == "running"), None)

    def effective(self, g: GateState) -> str:
        """The gate's state, with an unfinished gate in a died job read as `died`."""
        return "died" if not g.finished and self.state == "died" else g.state


def root() -> str:
    return os.path.join(git.git_dir(), "tangier", "jobs")


@contextlib.contextmanager
def lock() -> Generator[None]:
    """Hold the jobs lock, so checking for a running job and creating one is one step."""
    os.makedirs(root(), exist_ok=True)
    with open(os.path.join(root(), "lock"), "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def create(argv: list[str], base: str, snap: gate.Snapshot, keys: dict[str, str]) -> Job:
    """A new job for the gates in `keys`, in order, each pending. Call under `lock()`.

    Refuses while another job is running. See `[job-one-per-worktree]`.
    """
    running = next((j for j in all_jobs() if j.state == "running"), None)
    if running is not None:
        current = running.current()
        what = f" ({current.name})" if current else ""
        raise JobError(
            f"job {running.id} is running{what}. Run `tangier gate wait --job {running.id}` or `tangier gate cancel`."
        )
    os.makedirs(root(), exist_ok=True)
    job_id = _next_id()
    job = Job(
        dir=os.path.join(root(), str(job_id)),
        id=job_id,
        pid=0,
        argv=argv,
        base=base,
        head=snap.commit,
        tree=snap.tree,
        dirty=snap.dirty,
        started=gate.now().isoformat(),
        gates=[GateState(name, key) for name, key in keys.items()],
    )
    os.makedirs(job.dir)
    open(job.log, "wb").close()
    _write_job(job)
    _write_gates(job.dir, job.gates)
    _ = prune_auto()
    return job


def hold_alive(job: Job) -> int:
    """Lock the job's `alive` file and return the descriptor, to hand to the job process.

    The lock lives as long as any process holds the descriptor, so the job
    reads as alive from before it is spawned until it exits, however it exits.
    A reused pid cannot fake it. Close this copy once the job holds its own.
    """
    fd = os.open(os.path.join(job.dir, "alive"), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def alive(job_dir: str) -> bool:
    """Whether some process still holds the job's `alive` lock."""
    try:
        fd = os.open(os.path.join(job_dir, "alive"), os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def set_pid(job: Job, pid: int) -> None:
    job.pid = pid
    _write_job(job)


def _next_id() -> int:
    path = os.path.join(root(), "next-id")
    try:
        with open(path) as fh:
            job_id = int(fh.read().strip() or 1)
    except (FileNotFoundError, ValueError):
        job_id = max(_ids(), default=0) + 1
    _write_atomic(path, str(job_id + 1))
    return job_id


def _ids() -> list[int]:
    try:
        names = os.listdir(root())
    except FileNotFoundError:
        return []
    return sorted(int(n) for n in names if n.isdigit())


def load(job_dir: str) -> Job:
    """Read the job, `done` before `gates.json`: the job writes them in the other order."""
    with open(os.path.join(job_dir, "job.json")) as fh:
        job = Job(dir=job_dir, **json.load(fh))
    job.code = _read_done(job_dir)
    job.gates = _read_gates(job_dir)
    if job.code is None and not alive(job_dir):
        # It may have finished since `done` was read.
        job.code = _read_done(job_dir)
        job.gates = _read_gates(job_dir)
    if job.code is None:
        job.state = "running" if alive(job_dir) else "died"
    elif any(g.state == "cancelled" for g in job.gates):
        job.state = "cancelled"
    else:
        job.state = "passed" if job.code == 0 else "failed"
    return job


def _read_done(job_dir: str) -> int | None:
    try:
        with open(os.path.join(job_dir, "done")) as fh:
            return int(fh.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def find(job_id: int) -> Job | None:
    path = os.path.join(root(), str(job_id))
    try:
        return load(path)
    except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
        return None


def all_jobs() -> list[Job]:
    """Every readable job in this worktree, newest first."""
    found = (find(job_id) for job_id in reversed(_ids()))
    return [job for job in found if job is not None]


def latest() -> Job | None:
    jobs = all_jobs()
    return jobs[0] if jobs else None


def _write_job(job: Job) -> None:
    data = {k: v for k, v in asdict(job).items() if k not in ("dir", "gates", "code", "state")}
    _write_atomic(os.path.join(job.dir, "job.json"), json.dumps(data, indent=2))


def _read_gates(job_dir: str) -> list[GateState]:
    with open(os.path.join(job_dir, "gates.json")) as fh:
        return [GateState(**g) for g in json.load(fh)]


def _write_gates(job_dir: str, gates: list[GateState]) -> None:
    _write_atomic(os.path.join(job_dir, "gates.json"), json.dumps([asdict(g) for g in gates], indent=2))


def _write_atomic(path: str, text: str) -> None:
    """Write via a temporary file and a rename, so a reader never sees half a file."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        _ = fh.write(text)
    os.replace(tmp, path)


def finish(job_dir: str, code: int, unfinished: str = "error") -> bool:
    """Give each unfinished gate the state `unfinished`, then write `done`. Nothing in the job changes after it.

    Returns False, and changes nothing, when the job has already finished.
    """
    if _read_done(job_dir) is not None:
        return False
    gates = _read_gates(job_dir)
    now = gate.now().isoformat()
    for g in gates:
        if not g.finished:
            g.state, g.ended = unfinished, now
    _write_gates(job_dir, gates)
    _write_atomic(os.path.join(job_dir, "done"), str(code))
    return True


class Reporter:
    """Receives each gate's progress from an inline run. This base class discards it: CI and dry runs use it."""

    def offset(self) -> int:
        """Where the output printed from now on starts in the log."""
        return 0

    def started(self, name: str, key: str, offset: int) -> None:
        del name, key, offset

    def finished(self, name: str, state: str, code: int, *, line: str = "", ref: str | None = None) -> None:
        del name, state, code, line, ref


class JobReporter(Reporter):
    """Keeps `gates.json` up to date, from inside the job process."""

    def __init__(self, job_dir: str) -> None:
        self.dir = job_dir
        self.gates = _read_gates(job_dir)

    def _gate(self, name: str) -> GateState:
        found = next((g for g in self.gates if g.name == name), None)
        if found is None:
            found = GateState(name, "")
            self.gates.append(found)
        return found

    def offset(self) -> int:
        sys.stdout.flush()
        sys.stderr.flush()
        return os.path.getsize(os.path.join(self.dir, "output.log"))

    def started(self, name: str, key: str, offset: int) -> None:
        g = self._gate(name)
        g.state, g.key, g.offset, g.started = "running", key, offset, gate.now().isoformat()
        _write_gates(self.dir, self.gates)

    def finished(self, name: str, state: str, code: int, *, line: str = "", ref: str | None = None) -> None:
        g = self._gate(name)
        if g.started is None:
            # The gate failed before it started, as when it cannot be keyed.
            g.offset, g.started = self.offset(), gate.now().isoformat()
        g.state, g.code, g.line, g.ref, g.ended = state, code, line, ref, gate.now().isoformat()
        g.end = self.offset()
        _write_gates(self.dir, self.gates)


def cancel(job: Job, runner: Runner) -> bool:
    """Stop the job's process group: SIGTERM, then SIGKILL after `CANCEL_GRACE` seconds.

    Every unfinished gate becomes `cancelled`. See `[cancel]` for a gate that
    escapes the signal. Returns False when the job finished first, and leaves
    its result alone.
    """
    if alive(job.dir):
        _killpg(job.pid, signal.SIGTERM)
        waited = 0.0
        while alive(job.dir) and waited < CANCEL_GRACE:
            runner.sleep(CANCEL_POLL)
            waited += CANCEL_POLL
        if alive(job.dir):
            _killpg(job.pid, signal.SIGKILL)
        # The job must be gone before its files are rewritten.
        while alive(job.dir) and waited < 2 * CANCEL_GRACE:
            runner.sleep(CANCEL_POLL)
            waited += CANCEL_POLL
    return finish(job.dir, CANCELLED_CODE, unfinished="cancelled")


def _killpg(pid: int, sig: signal.Signals) -> None:
    if pid <= 0:
        return
    # macOS refuses to signal a group whose processes have all exited but are not yet reaped.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


def output(job: Job, name: str) -> str:
    """The gate's slice of the log: from its offset to its end, or while it runs, to the end of the log.

    Output after the last gate, such as the publish of its records, is in no gate's slice.
    """
    g = job.gate(name)
    if g.offset is None:
        return ""
    end = g.end
    with open(job.log, "rb") as fh:
        _ = fh.seek(g.offset)
        data = fh.read() if end is None else fh.read(end - g.offset)
    return data.decode(errors="replace")


def log_tail(job: Job, lines: int = 30) -> str:
    with open(job.log, "rb") as fh:
        return tail(fh.read().decode(errors="replace"), lines)


def tail(text: str, lines: int = 30) -> str:
    return "\n".join(text.rstrip("\n").splitlines()[-lines:])


def current_keys(cfg: Config, names: list[str]) -> tuple[str, dict[str, str | None]]:
    """The working tree now, and each gate's key for it. A gate that cannot be keyed has None."""
    tree = gate.snapshot().tree
    keys: dict[str, str | None] = {}
    for name in names:
        try:
            keys[name] = gate.key(cfg, name, tree)
        except gate.GateError:
            keys[name] = None
    return tree, keys


def drifted(job: Job, keys: dict[str, str | None]) -> list[str]:
    """The running gates whose key no longer matches the working tree: their pass will not be recorded."""
    return [g.name for g in job.gates if g.state == "running" and keys.get(g.name) != g.key]


def stale(g: GateState, keys: dict[str, str | None]) -> bool:
    """Whether a finished gate's result no longer applies to the working tree."""
    return g.finished and g.state not in ("cancelled", "died") and keys.get(g.name) != g.key


def prune_auto() -> list[int]:
    """Delete finished jobs older than `KEEP_FOR`, then the oldest while the logs pass `MAX_LOG_BYTES`.

    The newest `KEEP_LATEST` jobs and any running job always stay.
    """
    jobs = all_jobs()
    candidates = [j for j in jobs[KEEP_LATEST:] if j.state != "running"]
    cutoff = gate.now() - KEEP_FOR
    old = [j for j in candidates if _started(j) < cutoff]
    kept = [j for j in candidates if j not in old]
    total = sum(_size(j.log) for j in jobs if j not in old)
    # Oldest first.
    for j in reversed(kept):
        if total <= MAX_LOG_BYTES:
            break
        old.append(j)
        total -= _size(j.log)
    return _delete(old)


def _delete(jobs: list[Job]) -> list[int]:
    for j in jobs:
        shutil.rmtree(j.dir, ignore_errors=True)
    return sorted(j.id for j in jobs)


def _started(job: Job) -> datetime:
    return datetime.fromisoformat(job.started)


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except FileNotFoundError:
        return 0
