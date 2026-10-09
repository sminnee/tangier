"""Gate jobs: a `gate run` that outlives the shell that started it. See `docs/specs/gate.md#jobs`.

Every job on the machine is a directory under `~/.tangier/jobs/<id>/` (see `home`):

  job.json        what ran: id, pid, argv, gates, base, the commit and tree, dirty, started,
                  and the worktree and git directory it ran in
  gates.json      each gate's state, start and end, exit code, key, and where its output starts
  output.log      the job's combined stdout and stderr
  alive           locked for as long as the job process lives
  procs           locked by the job process and every command it runs
  slot            written when the job leaves the queue and holds a slot
  cancelled.json  who cancelled the job, and why; written by `gate cancel`
  ended.json      written when a reaper finalized a job whose process died
  done            written last, holding the job's exit code

At most `max-running` jobs hold a slot, a lock on `~/.tangier/slots/<i>`. The
rest wait in a queue, oldest first. Liveness comes from kernel locks only,
never a bare pid: a lock is released when its process dies, however it dies.

The job process writes `gates.json` through a `Reporter`. Every other command
only reads the directory, apart from `cancel`, `reap`, `prune` and the migration.
"""

from __future__ import annotations

import contextlib
import fcntl
import getpass
import json
import os
import shutil
import signal
import sys
from collections.abc import Generator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from tangier import gate, git, home
from tangier.config import Config
from tangier.runner import Runner

# A gate state that will not change again.
FINISHED = ("passed", "failed", "not-needed", "verified", "unrecorded", "error", "cancelled", "died")
# The finished states that count as a pass.
OK = ("passed", "not-needed", "verified")

# A job that has not finished. `stopping`: a cancel is stopping it. `orphaned`: the job process is
# gone, but commands it ran are not.
ACTIVE = ("queued", "running", "stopping")
UNFINISHED = (*ACTIVE, "orphaned")

# The automatic prune's limits. See `prune_auto`.
KEEP_LATEST = 5
KEEP_FOR = timedelta(hours=24)
MAX_LOG_BYTES = 200 * 1024 * 1024

# How often a queued job looks for a free slot.
QUEUE_POLL = 1

# How long `cancel` waits after SIGTERM before it sends SIGKILL.
CANCEL_GRACE = 10
CANCEL_POLL = 0.5

# The exit code a cancelled job records in `done`, as a shell does for SIGINT.
CANCELLED_CODE = 130


class JobError(gate.GateError):
    """A job cannot be started or found."""


class JobBusy(JobError):
    """Another job is running in this worktree. See `[job-one-per-worktree]`."""

    def __init__(self, job: Job) -> None:
        super().__init__(f"job {job.id} is running, so this run did not start.\n{wait_hint(job)}")
        self.job = job


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
    # A `ranon.load()` sample from when the gate finished.
    load: dict[str, float | int] | None = None

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
    # The worktree's top directory and its git directory, both resolved. Empty in a job from before they were kept.
    worktree: str = ""
    git_dir: str = ""
    gates: list[GateState] = field(default_factory=list)
    # The exit code from `done`, or None while the job has not finished.
    code: int | None = None
    # `queued`, `running`, `orphaned`, `died`, `cancelled`, `passed` or `failed`, as `load` read it.
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

    def phase(self) -> str:
        """A job's phase: `queued (...)`, `<gate> (<time>)`, `starting`, or after its gates `publishing records`.

        A queued job's phase is its place in the queue and the slots in use.
        """
        if self.state == "queued":
            position, length = queue_position(self) or (0, 0)
            limit = home.load_config().max_running
            return f"queued ({position} of {length}; {slots_in_use()} of {limit} slots in use)"
        current = self.current()
        if current is not None and current.started:
            return f"{current.name} ({running_for(current.started, None)})"
        if self.gates and all(g.finished for g in self.gates):
            return "publishing records"
        return "starting"

    def effective(self, g: GateState) -> str:
        """The gate's state, with an unfinished gate in a died job read as `died`."""
        return "died" if not g.finished and self.state == "died" else g.state


def root() -> str:
    return os.path.join(home.root(), "jobs")


def _slots() -> str:
    return os.path.join(home.root(), "slots")


def here() -> str:
    """This worktree's git directory, resolved, as a job's `git_dir` holds it."""
    return os.path.realpath(git.git_dir())


@contextlib.contextmanager
def lock() -> Generator[None]:
    """Hold the machine-wide jobs lock, so checking the jobs and changing them is one step. It is not re-entrant."""
    home.makedirs(root())
    path = os.path.join(root(), "lock")
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except PermissionError as e:
        raise home.unwritable(path, e) from e
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def wait_hint(job: Job) -> str:
    """What to run about a job left running. Every path that leaves one running prints this. See `[job-wait-hint]`."""
    return (
        f"job {job.id} is in the background: {job.phase()}.\n"
        f"Run `tangier gate wait --job {job.id}` to keep waiting (up to an hour; exits 0 if every gate passed, "
        "1 if one failed or it was cancelled, 3 if still queued or running).\n"
        "`tangier gate status` shows progress; `tangier gate cancel` stops it.\n"
        f"Full output: {home.tilde(job.log)}"
    )


def took(seconds: float) -> str:
    """A run's length: `12.3s` under a minute, `4m05s` from a minute up."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m{rest:02d}s"


def running_for(start: str | None, end: str | None) -> str:
    """How long a gate ran, as `took` gives it. Without `end`, up to now."""
    if start is None:
        return ""
    finish = datetime.fromisoformat(end) if end else gate.now()
    return took(max(0.0, (finish - datetime.fromisoformat(start)).total_seconds()))


def running() -> Job | None:
    """The unfinished job in this worktree, queued or running, if there is one."""
    return next((j for j in all_jobs() if j.state in UNFINISHED), None)


def create(argv: list[str], base: str, snap: gate.Snapshot, keys: dict[str, str]) -> Job:
    """A new job for the gates in `keys`, in order, each pending. Call under `lock()`.

    Raises `JobBusy` while another job in this worktree has not finished. See `[job-one-per-worktree]`.
    """
    busy = running()
    if busy is not None:
        raise JobBusy(busy)
    home.makedirs(root())
    _ = migrate_legacy()
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
        worktree=os.path.realpath(git.toplevel()),
        git_dir=here(),
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
    return _hold(os.path.join(job.dir, "alive"))


def hold_procs(job_dir: str) -> int:
    """Lock the job's `procs` file and return the descriptor, for the job process to pass to each command it runs.

    The commands and what they start inherit it, so the lock outlives a job
    process that dies while its commands run. See `[job-reap]`.
    """
    return _hold(os.path.join(job_dir, "procs"))


def _hold(path: str) -> int:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def alive(job_dir: str) -> bool:
    """Whether some process still holds the job's `alive` lock."""
    return _held(os.path.join(job_dir, "alive"))


def procs(job_dir: str) -> bool:
    """Whether some command the job ran still holds its `procs` lock."""
    return _held(os.path.join(job_dir, "procs"))


def _busy(job_dir: str) -> bool:
    """Whether any process of the job is left: the job process, or a command it ran."""
    return alive(job_dir) or procs(job_dir)


def _held(path: str) -> bool:
    try:
        fd = os.open(path, os.O_RDONLY)
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
        if _held(os.path.join(job_dir, "stopping")):
            job.state = "stopping"
        elif alive(job_dir):
            job.state = "running" if has_slot(job_dir) else "queued"
        else:
            job.state = "orphaned" if procs(job_dir) else "died"
    elif os.path.exists(os.path.join(job_dir, "ended.json")):
        job.state = "died"
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


def all_jobs(*, worktree_only: bool = True) -> list[Job]:
    """Every readable job in this worktree, or with `worktree_only=False` on the machine, newest first."""
    mine = here() if worktree_only else None
    found = (find(job_id) for job_id in reversed(_ids()))
    return [job for job in found if job is not None and (mine is None or job.git_dir == mine)]


def latest() -> Job | None:
    jobs = all_jobs()
    return jobs[0] if jobs else None


def cancelled_info(job: Job) -> dict[str, str] | None:
    """Who cancelled the job, with what, why and when, as `gate cancel` wrote it. None for no such file."""
    try:
        with open(os.path.join(job.dir, "cancelled.json")) as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


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

    def finished(
        self,
        name: str,
        state: str,
        code: int,
        *,
        line: str = "",
        ref: str | None = None,
        load: dict[str, float | int] | None = None,
    ) -> None:
        del name, state, code, line, ref, load


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

    def finished(
        self,
        name: str,
        state: str,
        code: int,
        *,
        line: str = "",
        ref: str | None = None,
        load: dict[str, float | int] | None = None,
    ) -> None:
        g = self._gate(name)
        if g.started is None:
            # The gate failed before it started, as when it cannot be keyed.
            g.offset, g.started = self.offset(), gate.now().isoformat()
        g.state, g.code, g.line, g.ref, g.ended = state, code, line, ref, gate.now().isoformat()
        g.load = load
        g.end = self.offset()
        _write_gates(self.dir, self.gates)


def cancel(targets: list[Job], runner: Runner, *, by: str | None = None, reason: str | None = None) -> list[Job]:
    """Stop each job's process group: SIGTERM, then SIGKILL after `CANCEL_GRACE` seconds. Returns the jobs it cancelled.

    `by` and `reason` go into each job's `cancelled.json` for its waiter. A Ctrl-C passes no `by`.
    A job that finished first, or that another cancel is stopping, is left alone. See `[cancel]`.

    While it signals and waits it holds each job's `stopping` lock, not the jobs lock: the job
    reads as `stopping`, so no reap reads it as died and it has no place in the queue, and every
    other command carries on. A cancel that dies frees the locks, and the jobs are reaped.
    """
    stopping: list[int] = []
    try:
        with lock():
            mine = []
            # Read again under the lock: the pid may have been set, or the job finished, since `targets` was read.
            for j in (load(j.dir) for j in targets if _read_done(j.dir) is None):
                fd = _try_hold(os.path.join(j.dir, "stopping"))
                if fd is not None:
                    stopping.append(fd)
                    mine.append(j)
            if by is not None:
                info = {"by": by, "reason": reason, "user": _user(), "time": gate.now().isoformat()}
                for j in mine:
                    _write_atomic(os.path.join(j.dir, "cancelled.json"), json.dumps(info, indent=2))
        _stop(mine, runner)
        with lock():
            cancelled = []
            for j in mine:
                if finish(j.dir, CANCELLED_CODE, unfinished="cancelled"):
                    cancelled.append(j)
                elif by is not None:
                    with contextlib.suppress(FileNotFoundError):
                        os.remove(os.path.join(j.dir, "cancelled.json"))
            return cancelled
    finally:
        for fd in stopping:
            os.close(fd)


def _try_hold(path: str) -> int | None:
    """Lock `path` and return the descriptor, or None when another process holds it."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _user() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return ""


def _stop(targets: list[Job], runner: Runner) -> None:
    """SIGTERM each job's group, then SIGKILL it until no process of the job is left. Each phase lasts `CANCEL_GRACE`.

    A job is gone once both its `alive` and `procs` locks are free. One that
    still holds one after both grace periods, as a command that left the group
    but kept the lock, is left to itself.
    """
    for j in targets:
        if _busy(j.dir):
            _killpg(j.pid, signal.SIGTERM)
    waited = 0.0
    while any(_busy(j.dir) for j in targets) and waited < CANCEL_GRACE:
        runner.sleep(CANCEL_POLL)
        waited += CANCEL_POLL
    # SIGKILL on each poll, for a command started in the group after the last one.
    while (left := [j for j in targets if _busy(j.dir)]) and waited < 2 * CANCEL_GRACE:
        for j in left:
            _killpg(j.pid, signal.SIGKILL)
        runner.sleep(CANCEL_POLL)
        waited += CANCEL_POLL


def reap(runner: Runner) -> list[Job]:
    """Finalize every job whose process died, after killing the commands it left. Returns those jobs.

    Every command that reads jobs reaps first, so a dead job never keeps a
    slot or a place in the queue. See `[job-reap]`.
    """
    if not _ids():
        return []
    with lock():
        return _reap_locked(runner)


def _reap_locked(runner: Runner) -> list[Job]:
    dead = [j for j in all_jobs(worktree_only=False) if j.state in ("orphaned", "died") and j.code is None]
    # Kill first and write after, so a reap stopped part way can run again.
    _stop([j for j in dead if j.state == "orphaned"], runner)
    reaped = []
    for j in dead:
        # `ended.json` before `done`, which is written last, so the job never reads `failed` between the two.
        ended = {"by": "reaper", "reason": "job process died", "time": gate.now().isoformat()}
        _write_atomic(os.path.join(j.dir, "ended.json"), json.dumps(ended, indent=2))
        _ = finish(j.dir, 1, unfinished="died")
        reaped.append(j)
    return reaped


def claim_slot(job_dir: str, runner: Runner, max_running: int) -> int:
    """Wait in the queue until this job may run, then hold a slot. Returns the slot's locked descriptor.

    Keep it open while the job runs. Each poll reaps first. See `[job-queue]`.
    """
    while True:
        with lock():
            _ = _reap_locked(runner)
            fd = claim_free(job_dir, max_running)
        if fd is not None:
            return fd
        runner.sleep(QUEUE_POLL)


def has_slot(job_dir: str) -> bool:
    """Whether the job holds a slot: its parent took one before the spawn, or it took one itself."""
    return os.path.exists(os.path.join(job_dir, "slot"))


def claim_free(job_dir: str, max_running: int) -> int | None:
    """A free slot's locked descriptor, when this job is first in the queue, or None. Call under `lock()`."""
    first = next(iter(_queue()), None)
    if first is None or first.id != int(os.path.basename(job_dir)):
        return None
    home.makedirs(_slots())
    for i in range(max_running):
        fd = _try_hold(os.path.join(_slots(), str(i)))
        if fd is None:
            continue
        try:
            _write_atomic(os.path.join(job_dir, "slot"), gate.now().isoformat())
        except BaseException:
            os.close(fd)
            raise
        return fd
    return None


def _queue() -> list[Job]:
    """Every queued job on the machine, oldest first."""
    return [j for j in reversed(all_jobs(worktree_only=False)) if j.state == "queued"]


def queue_position(job: Job) -> tuple[int, int] | None:
    """The queued job's place in the queue, from 1, and the queue's length. None when it is not queued."""
    queue = [j.id for j in _queue()]
    return (queue.index(job.id) + 1, len(queue)) if job.id in queue else None


def slots_in_use() -> int:
    """How many jobs on the machine hold a slot."""
    return sum(1 for j in all_jobs(worktree_only=False) if j.state == "running")


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


def after_gates(job: Job) -> str:
    """The log after the last gate's slice: what the job printed once its gates were done, as a failed publish.

    Empty when a gate has no end, as one cut off by a cancel: its output has no bound.
    """
    ends = [g.end for g in job.gates]
    if not ends or None in ends:
        return ""
    with open(job.log, "rb") as fh:
        _ = fh.seek(max(e for e in ends if e is not None))
        return fh.read().decode(errors="replace")


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
    """Delete finished jobs past `KEEP_FOR` or whose worktree is gone, then the oldest while logs pass `MAX_LOG_BYTES`.

    The newest `KEEP_LATEST` jobs of each worktree, unless it is gone, and
    every unfinished job always stay. See `[job-prune]`.
    """
    jobs = all_jobs(worktree_only=False)
    newest: dict[str, int] = {}
    latest: set[int] = set()
    for j in jobs:
        newest[j.git_dir] = newest.get(j.git_dir, 0) + 1
        if newest[j.git_dir] <= KEEP_LATEST:
            latest.add(j.id)
    finished = [j for j in jobs if j.state not in UNFINISHED]
    gone = [j for j in finished if j.worktree and not os.path.isdir(j.worktree)]
    cutoff = gate.now() - KEEP_FOR
    old = gone + [j for j in finished if j not in gone and j.id not in latest and _started(j) < cutoff]
    deleted = {j.id for j in old}
    kept = [j for j in finished if j.id not in deleted and j.id not in latest]
    total = sum(_size(j.log) for j in jobs if j.id not in deleted)
    # Oldest first.
    for j in reversed(kept):
        if total <= MAX_LOG_BYTES:
            break
        old.append(j)
        total -= _size(j.log)
    return _delete(old)


def migrate_legacy() -> list[int]:
    """Move this worktree's jobs from `<git-dir>/tangier/jobs/` into `root()`, with new IDs. Returns the new IDs.

    Call under `lock()`. It also holds the legacy folder's own lock, so an
    older tangier cannot create a job there meanwhile. A legacy job still
    running stays, as its process writes to its old path, and moves on a later
    run. The legacy folder goes once it holds no job. See `[job-migrate]`.
    """
    legacy = os.path.join(git.git_dir(), "tangier", "jobs")
    if not os.path.isdir(legacy):
        return []
    with open(os.path.join(legacy, "lock"), "a") as legacy_lock:
        fcntl.flock(legacy_lock, fcntl.LOCK_EX)
        return _migrate(legacy)


def _migrate(legacy: str) -> list[int]:
    moved = []
    worktree, mine = os.path.realpath(git.toplevel()), here()
    for old_id in sorted(int(n) for n in os.listdir(legacy) if n.isdigit()):
        src = os.path.join(legacy, str(old_id))
        if alive(src):
            continue
        try:
            with open(os.path.join(src, "job.json")) as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            # Unreadable, so no command would show it.
            shutil.rmtree(src, ignore_errors=True)
            continue
        job_id = _next_id()
        # Rewritten before the move, so a moved job always has its new ID and worktree.
        data.update(id=job_id, worktree=worktree, git_dir=mine)
        _write_atomic(os.path.join(src, "job.json"), json.dumps(data, indent=2))
        _ = shutil.move(src, os.path.join(root(), str(job_id)))
        moved.append(job_id)
    if not any(n.isdigit() for n in os.listdir(legacy)):
        shutil.rmtree(legacy, ignore_errors=True)
        with contextlib.suppress(OSError):
            os.rmdir(os.path.dirname(legacy))
    return moved


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
