"""`tangier gate ...` — record a local gate pass, and find it again in CI."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import signal
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta

import tangier
from tangier import gate, git, jobs, junit, ranon
from tangier.commands.args import add_diff_args, add_full
from tangier.config import Config, GateSpec, gate_groups, gate_output_name
from tangier.github import emit_outputs, write_summary
from tangier.runner import Runner, Subprocess

# The run clock. Tests patch this.
clock = time.monotonic


def _runner(args: argparse.Namespace) -> Runner:
    return getattr(args, "runner", None) or Subprocess(echo=True)


def cmd_list(config: Config, args: argparse.Namespace) -> int:
    """Print each gate in config order, with a group's members indented under it."""
    groups = gate_groups(config)
    for name, spec in config.gates.items():
        if not spec.group:
            print(name)
        elif groups[spec.group][0] == name:
            print(f"{spec.group} (group)")
            for member in groups[spec.group]:
                print(f"  {member}")
    return 0


def cmd_key(config: Config, args: argparse.Namespace) -> int:
    """Print the key. A group, or no name, prints `<name> <key>` for each gate."""
    names = list(config.gates) if args.name is None else gate.select(config, args.name)
    tree = gate.snapshot(args.head).tree
    if args.name in config.gates:
        print(gate.key(config, args.name, tree))
        return 0
    # Every key before any output, so a gate that fails closed leaves no partial list.
    keys = [(name, gate.key(config, name, tree)) for name in names]
    for name, key in keys:
        print(f"{name} {key}")
    return 0


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    """Run each selected gate on the working tree, unless the diff from its comparator does not need it.

    The key describes the working tree, uncommitted work included. `--dry-run`
    runs nothing. A failing gate does not stop the rest, unless `--fail-fast`.
    The exit code is the first non-zero one.

    Once every gate has run, the records this invocation wrote are published
    to origin. A failed publish is a warning: the records stay local, and the
    exit code is still the gates'.

    Outside CI the gates run in a background job. A plain `gate run` returns
    once it has started, and `--wait` waits for it, up to an hour. See
    `docs/specs/gate.md#jobs`.
    """
    if args.job_dir:
        return _run_as_job(config, args)
    names = _selected(config, args)
    if _in_background(args):
        return _start_job(config, args, names)
    return _run_inline(config, args, names, jobs.Reporter())


def _in_background(args: argparse.Namespace) -> bool:
    """Whether `gate run` starts a job: not in CI, and not for `--dry-run`. See `[job-inline-ci]`."""
    return not (args.dry_run or ranon.detect()["kind"] == "ci")


def _run_inline(config: Config, args: argparse.Namespace, names: list[str], reporter: jobs.Reporter) -> int:
    # One read of origin for all gates, and none when every record is local.
    origin = gate.OriginRecords()
    first = 0
    written: set[str] = set()
    for i, name in enumerate(names):
        try:
            code, ref = _run_one(config, args, name, origin, reporter)
        except (gate.GateError, git.GitError) as e:
            # Printed first, so the gate's slice of the job log holds the error.
            print(f"error: {e}", file=sys.stderr)
            reporter.finished(name, "error", 2, line=f"gate `{name}`: error: {e}")
            code, ref = 2, None
        first = first or code
        if ref:
            written.add(ref)
        if first and args.fail_fast:
            if rest := names[i + 1 :]:
                print(f"--fail-fast: not run: {', '.join(rest)}", file=sys.stderr)
            for skipped in rest:
                reporter.finished(
                    skipped, "cancelled", jobs.CANCELLED_CODE, line=f"gate `{skipped}`: not run (--fail-fast)"
                )
            break
    if written:
        try:
            synced = gate.publish(written)
        except (gate.GateError, git.GitError) as e:
            print(f"warning: gate records stay local ({e}); run `tangier gate sync` to retry", file=sys.stderr)
        else:
            _print_synced(synced, quiet=True)
    return first


def _run_as_job(config: Config, args: argparse.Namespace) -> int:
    """The job process: the inline run, reporting to the job directory, then `done`.

    A gate still unfinished when `done` is written, after an unexpected error, ends `error`.
    """
    code = 2
    try:
        code = _run_inline(config, args, _selected(config, args), jobs.JobReporter(args.job_dir))
    except gate.GateError as e:
        print(f"error: {e}", file=sys.stderr)
    finally:
        sys.stdout.flush()
        jobs.finish(args.job_dir, code)
    return code


def _start_job(config: Config, args: argparse.Namespace, names: list[str]) -> int:
    """Start a job for `names`, then wait for it up to the run's limit, as `gate wait` does. Ctrl-C cancels it.

    A job already running is waited out first, inside the same limit. See `[job-one-per-worktree]`.
    """
    waiter = _Waiter(_runner(args), _limit(args, WAIT_LIMIT if args.wait else 0), cancels=True)

    def start_and_wait() -> int:
        job, alive = _create_when_free(config, args, names, waiter)
        waiter.started = job
        try:
            jobs.set_pid(
                job, waiter.runner.spawn(_job_argv(args.argv, job.dir), log=job.log, env=_job_env(), pass_fds=(alive,))
            )
        finally:
            os.close(alive)
        print(f"job {job.id}: {', '.join(names)} ({job.commit_label})", flush=True)
        _warn_load()
        return _wait(config, [job], None, waiter, run=True)

    return waiter.run(start_and_wait)


class _Waiter:
    """A waiting command's limit, and what SIGTERM, SIGHUP and Ctrl-C do to it. See `[job-wait-hint]`.

    SIGTERM and SIGHUP, which a tool timeout or a closed shell sends, only set
    `stopped`. The poll loops read it between sleeps, so a signal never
    interrupts a job's creation, its spawn or a file write. A signal the caller
    ignores, as under `nohup`, stays ignored.

    A Ctrl-C in `gate run` cancels the job it `started`, and before one has
    started it only stops the wait, as it does in `gate wait`. Each exits 130.
    """

    def __init__(self, runner: Runner, limit: float | None, *, cancels: bool) -> None:
        self.runner = runner
        self.deadline = None if limit is None else clock() + limit
        self.cancels = cancels
        self.started: jobs.Job | None = None
        self.stopped = False

    def left(self) -> float | None:
        """Seconds until the limit, never below 0. None for no limit."""
        return None if self.deadline is None else max(0.0, self.deadline - clock())

    def done(self) -> bool:
        """Whether to stop waiting: the limit has passed, or a signal asked to stop."""
        return self.stopped or self.left() == 0

    def run(self, body: Callable[[], int]) -> int:
        """`body`'s exit code, with SIGTERM and SIGHUP recorded while it runs, and a Ctrl-C's exit code."""

        def stop(signum: int, frame: object) -> None:
            del signum, frame
            self.stopped = True

        old = {
            sig: signal.signal(sig, stop)
            for sig in (signal.SIGTERM, signal.SIGHUP)
            if signal.getsignal(sig) is not signal.SIG_IGN
        }
        try:
            return body()
        except KeyboardInterrupt:
            if self.cancels and self.started is not None:
                return _cancel_started(self.started, self.runner)
            what = "this run did not start" if self.cancels else "the job carries on"
            print(f"stopped waiting; {what}", file=sys.stderr)
            return jobs.CANCELLED_CODE
        finally:
            for sig, previous in old.items():
                _ = signal.signal(sig, previous)


def _create_when_free(
    config: Config, args: argparse.Namespace, names: list[str], waiter: _Waiter
) -> tuple[jobs.Job, int]:
    """Create the job, and lock its `alive` file, once no other job runs. Returns the job and the lock.

    A running job is waited for, quietly, until the waiter is done. Then this raises `jobs.JobBusy`.
    """
    announced = False
    while True:
        # Keyed afresh on each try: the tree may have changed while the other job ran.
        snap = gate.snapshot()
        keys = {name: _key_or_blank(config, name, snap.tree) for name in names}
        with jobs.lock():
            try:
                job = jobs.create(args.argv, args.base, snap, keys)
            except jobs.JobBusy as busy:
                other = busy.job
            else:
                # Under the lock, so the job reads as running before another run looks.
                try:
                    return job, jobs.hold_alive(job)
                except BaseException:
                    # A Ctrl-C here must not leave a job that reads as died.
                    _ = jobs.finish(job.dir, jobs.CANCELLED_CODE, unfinished="cancelled")
                    raise
        if not announced and waiter.left() != 0:
            print(f"job {other.id} is running: {other.phase()}; waiting for it before starting", flush=True)
            announced = True
        while (now := jobs.find(other.id)) is not None and now.state == "running":
            if waiter.done():
                raise jobs.JobBusy(now)
            waiter.runner.sleep(POLL)


def _job_env() -> dict[str, str]:
    """The job's environment: `-m tangier` finds this process's package, whatever the job's cwd and sys.path."""
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(tangier.__file__)))
    pythonpath = os.pathsep.join(p for p in (package_root, os.environ.get("PYTHONPATH")) if p)
    return {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONPATH": pythonpath}


def _warn_load() -> None:
    """Warn on stderr when the machine has more load than CPUs. See `[run-load]`."""
    sample = ranon.load()
    if ranon.overloaded(sample):
        print(
            f"warning: load average {sample['load']} on {sample['cpus']} CPUs; "
            "timeouts may come from load, not the code",
            file=sys.stderr,
        )


def _job_argv(argv: list[str], job_dir: str) -> list[str]:
    """The job process's command: this one, with `--job-dir` straight after `run`, ahead of any `--`."""
    at = argv.index("run", argv.index("gate")) + 1
    return [sys.executable, "-m", "tangier", *argv[:at], "--job-dir", job_dir, *argv[at:]]


def _cancel_started(job: jobs.Job, runner: Runner) -> int:
    """Cancel the job `gate run` started, unless it finished first."""
    if not jobs.cancel(jobs.load(job.dir), runner):
        return _exit_code(jobs.load(job.dir), None, run=True)
    print(f"cancelled job {job.id}", file=sys.stderr)
    return jobs.CANCELLED_CODE


def _key_or_blank(config: Config, name: str, tree: str) -> str:
    """The gate's key at job start. A gate that cannot be keyed fails in the job, which says why."""
    try:
        return gate.key(config, name, tree)
    except gate.GateError:
        return ""


def _selected(config: Config, args: argparse.Namespace) -> list[str]:
    if args.all and args.name:
        raise gate.GateError("name gates or pass `--all`, not both")
    if args.all:
        return list(config.gates)
    if not args.name:
        raise gate.GateError("name a gate to run, or pass `--all`")
    return gate.select_all(config, args.name)


def _run_one(
    config: Config, args: argparse.Namespace, name: str, origin: gate.OriginRecords, reporter: jobs.Reporter
) -> tuple[int, str | None]:
    """Plan one gate, then run it unless it is verified or not needed.

    Returns its exit code, and the ref it wrote or None.

    Each gate takes its own snapshot, because an earlier gate's commands can
    change the tree. `--full` reads no record and no diff.
    """

    def finished(
        state: str,
        code: int,
        line: str,
        *,
        ref: str | None = None,
        err: bool = False,
        load: dict[str, float | int] | None = None,
    ) -> tuple[int, str | None]:
        print(line, file=sys.stderr if err else sys.stdout)
        reporter.finished(name, state, code, line=line, ref=ref, load=load)
        return code, ref

    offset = reporter.offset()
    snap = gate.snapshot()
    if snap.dirty:
        touched = gate.uncommitted_in_scope(config, name, snap)
        what = (
            f"uncommitted changes in scope: {', '.join(touched)}"
            if touched
            else "no uncommitted change touches the scope"
        )
        print(f"gate `{name}`: keying the working tree; {what}", file=sys.stderr)
    p = gate.plan(config, name, args.base, snap, origin, full=args.full, accept=args.accept)
    reporter.started(name, p.key, offset)
    if args.debug:
        _print_debug(p)
    if args.dry_run:
        _print_plan(p)
        return 0, None
    if p.status == "not-needed":
        return finished("not-needed", 0, f"gate `{name}`: not-needed for this diff ({p.reason})")
    if p.status == "verified":
        return finished("verified", 0, f"gate `{name}`: verified ({p.reason}), nothing to run")

    spec = gate.spec_for(config, name)
    if spec.junit:
        # A report the commands do not write must not pass for theirs. See `[junit-stale]`.
        try:
            os.remove(spec.junit)
        except FileNotFoundError:
            pass
        except OSError as e:
            raise gate.GateError(f"gate `{name}`: cannot delete the old JUnit report at {spec.junit}: {e}") from e
    start = clock()
    code = _run_commands(_runner(args), spec, p.commands)
    duration = clock() - start
    # Sampled as the gate ends. See `[run-load]`.
    load = ranon.load() or None
    report = junit.read(spec.junit) if spec.junit else None
    if code != 0:
        line = f"gate `{name}`: failed in {jobs.took(duration)} (exit {code})"
        if args.read_only:
            return finished("failed", code, line, load=load)
        # Keyed as planned, before the run, whatever the commands left behind. See `[failure-record]`.
        ref = gate.write_failure(p, ranon.detect(), duration, load, code, report)
        return finished("failed", code, line, ref=ref, load=load)
    if args.read_only:
        return finished(
            "passed", 0, f"gate `{name}`: passed in {jobs.took(duration)}, no record written (--read-only)", load=load
        )
    # The key is content only, so a moved HEAD over the same tree is fine. A
    # changed tree is not: the commands did not test what the key describes.
    after = gate.snapshot().tree
    if after != snap.tree:
        return finished(
            "unrecorded",
            1,
            f"gate `{name}`: passed in {jobs.took(duration)}, but the working tree changed during the run "
            f"(tree {snap.tree[:7]}, now {after[:7]}), so no record was written",
            err=True,
            load=load,
        )
    ran_on = ranon.detect()
    ref = gate.write_record(p, ran_on, duration, load, report)
    return finished(
        "passed",
        0,
        f"gate `{name}`: passed in {jobs.took(duration)}, recorded as {ref} ({ran_on['kind']})",
        ref=ref,
        load=load,
    )


# How often the wait loop reads the job, and how many reads apart it re-keys the tree to look for drift.
POLL = 1
DRIFT_EVERY = 5
# The exit code of a wait that timed out with the job still running.
STILL_RUNNING = 3
# How long `gate run --wait` and `gate wait` wait, in seconds, before they leave the job running.
WAIT_LIMIT = 3600
# The hidden `--timeout`'s default: no value given.
_UNSET = -1


def _limit(args: argparse.Namespace, default: float) -> float | None:
    """Seconds to wait: the hidden `--timeout` when given, else `default`. None for no limit."""
    return default if args.timeout == _UNSET else args.timeout


def _wait(
    config: Config,
    waiting: list[jobs.Job],
    names: list[str] | None,
    waiter: _Waiter,
    *,
    run: bool = False,
) -> int:
    """Wait until each job is done, or the gates in `names` are, or the waiter is.

    On a terminal the job log streams. Anywhere else only each gate's start
    and result print, with the tail of a failure, which keeps an agent's
    context small. The limit is wall-clock time by `clock`, so slow reads and
    drift checks count toward it. A done waiter leaves the jobs running: the hint, exit 3.
    """
    stream = sys.stdout.isatty()
    printed: dict[int, int] = {job.id: 0 for job in waiting}
    reported: set[tuple[int, str]] = set()
    started: set[tuple[int, str]] = set()
    drift_warned: set[tuple[int, str]] = set()
    polls = 0
    while True:
        now = [_reload(job) for job in waiting]
        for job in now:
            if stream:
                printed[job.id] = _stream_log(job, printed[job.id], whole=_settled(job, names))
                _note_loads(job, _among(job, names), reported)
            else:
                _report_results(job, _among(job, names), reported, started)
        if polls % DRIFT_EVERY == 0:
            _warn_drift(config, now, drift_warned)
        polls += 1
        if all(_settled(job, names) for job in now):
            for job in now:
                if stream and job.state == "died":
                    print(f"job {job.id} died: its process is gone, and it wrote no result")
            return max(_exit_code(job, names, run=run) for job in now)
        if waiter.done():
            if waiter.stopped:
                print("stopped waiting.")
            for job in now:
                if not _settled(job, names):
                    print(jobs.wait_hint(job))
            return STILL_RUNNING
        waiter.runner.sleep(POLL)


def _reload(job: jobs.Job) -> jobs.Job:
    found = jobs.find(job.id)
    if found is None:
        raise gate.GateError(f"job {job.id} was deleted while waiting for it")
    return found


def _among(job: jobs.Job, names: list[str] | None) -> list[jobs.GateState]:
    """The job's gates that `names` selects, or all of them."""
    return [g for g in job.gates if names is None or g.name in names]


def _settled(job: jobs.Job, names: list[str] | None) -> bool:
    return job.state != "running" or all(g.finished for g in _among(job, names))


def _exit_code(job: jobs.Job, names: list[str] | None, *, run: bool) -> int:
    """0 when every selected gate passed, was verified or was not needed, and 1 otherwise.

    `run` gives the job's own exit code, as an inline run does, except that 3
    would read as still running, so it becomes 1.
    """
    if run and names is None and job.code is not None:
        return 1 if job.code == STILL_RUNNING else job.code
    return 0 if all(job.effective(g) in jobs.OK for g in _among(job, names)) else 1


def _stream_log(job: jobs.Job, start: int, *, whole: bool) -> int:
    """Print the log from `start` on, and return where the next read starts.

    Until `whole`, it stops at the last full line, so no character is split across two reads.
    """
    with open(job.log, "rb") as fh:
        _ = fh.seek(start)
        data = fh.read()
    if not whole:
        data = data[: data.rfind(b"\n") + 1]
    _ = sys.stdout.write(data.decode(errors="replace"))
    _ = sys.stdout.flush()
    return start + len(data)


def _report_results(
    job: jobs.Job, gates: list[jobs.GateState], reported: set[tuple[int, str]], started: set[tuple[int, str]]
) -> None:
    """Print a gate's start when it is first seen running, and its result once, when first seen finished.

    A died job's log tail prints once.
    """
    died = False
    for g in gates:
        state = job.effective(g)
        if state == "running" and (job.id, g.name) not in started:
            started.add((job.id, g.name))
            print(f"gate `{g.name}`: started", flush=True)
        if state not in jobs.FINISHED or (job.id, g.name) in reported:
            continue
        reported.add((job.id, g.name))
        if state == "died":
            print(f"gate `{g.name}`: died: the job process is gone, and it wrote no result")
            died = True
            continue
        print(g.line or f"gate `{g.name}`: {state}")
        if state not in jobs.OK and state != "cancelled":
            # The output ends with the line just printed.
            _print_tail(jobs.tail(jobs.output(job, g.name).removesuffix(f"{g.line}\n")), job.log)
            _note_load(g)
    if died:
        _print_tail(jobs.log_tail(job), job.log)


def _note_loads(job: jobs.Job, gates: list[jobs.GateState], noted: set[tuple[int, str]]) -> None:
    """On a terminal, where the log streams: the load note for each gate first seen failed."""
    for g in gates:
        if g.finished and (job.id, g.name) not in noted:
            noted.add((job.id, g.name))
            _note_load(g)


def _note_load(g: jobs.GateState) -> None:
    """For a failed gate that ran with more load than CPUs, say its timeouts may come from load. See `[run-load]`."""
    if g.state == "failed" and g.load and ranon.overloaded(g.load):
        print(
            f"note: load average was {g.load['load']} on {g.load['cpus']} CPUs during this gate; "
            "if the failures are timeouts, rerun when load is lower",
            file=sys.stderr,
        )


def _print_tail(text: str, log: str) -> None:
    if text:
        print("  | " + text.replace("\n", "\n  | "))
    print(f"  log: {os.path.relpath(log)}")


def _warn_drift(config: Config, now: list[jobs.Job], warned: set[tuple[int, str]]) -> None:
    """Warn, once per gate, when a running gate's key no longer matches the working tree."""
    if not any(job.current() for job in now):
        return
    try:
        _, keys = jobs.current_keys(config, [g.name for job in now for g in job.gates])
    except gate.GateError:
        return
    for job in now:
        for name in jobs.drifted(job, keys):
            if (job.id, name) not in warned:
                warned.add((job.id, name))
                print(
                    f"warning: gate `{name}`: the working tree changed since it started, so its pass will not be "
                    "recorded",
                    file=sys.stderr,
                )


def _ago(when: str) -> str:
    seconds = max(0, int((gate.now() - datetime.fromisoformat(when)).total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size * (2 if unit == "d" else 1):
            return f"{seconds // size}{unit} ago"
    return f"{seconds}s ago"


def _jobs_named(ids: list[int] | None) -> list[jobs.Job]:
    """The listed jobs, or the latest one. A missing job is an error."""
    if ids is None:
        latest = jobs.latest()
        if latest is None:
            raise gate.GateError("no gate job in this worktree")
        return [latest]
    found = []
    for job_id in ids:
        job = jobs.find(job_id)
        if job is None:
            raise gate.GateError(f"no job {job_id} in this worktree (pruned?)")
        found.append(job)
    return found


def cmd_wait(config: Config, args: argparse.Namespace) -> int:
    """Wait for a job, or for some of its gates. Exits 0 passed, 1 failed, 2 usage, 3 still running."""
    waiting = _jobs_named(args.job)
    names = gate.select_all(config, args.name) if args.name else None
    if names is not None:
        missing = [n for n in names if not any(g.name == n for job in waiting for g in job.gates)]
        if missing:
            ids = ", ".join(str(job.id) for job in waiting)
            raise gate.GateError(f"gate {', '.join(missing)} is not in job {ids}")
    waiter = _Waiter(_runner(args), _limit(args, WAIT_LIMIT), cancels=False)
    return waiter.run(lambda: _wait(config, waiting, names, waiter))


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    """Each recent job and its gates, newest first, with what no longer applies to the working tree. Exits 0."""
    missing: list[int] = []
    if args.job is not None:
        shown = []
        for job_id in args.job:
            job = jobs.find(job_id)
            if job is None:
                missing.append(job_id)
            else:
                shown.append(job)
    else:
        every = jobs.all_jobs()
        cutoff = gate.now() - args.since
        shown = [j for j in every if j.state == "running" or datetime.fromisoformat(j.started) >= cutoff]
        if not shown and every:
            shown = [every[0]]
    try:
        tree, keys = jobs.current_keys(config, sorted({g.name for job in shown for g in job.gates}))
    except gate.GateError:
        tree, keys = None, None
    if args.json:
        data: list[dict[str, object]] = [_job_json(job, tree, keys) for job in shown]
        data += [{"id": job_id, "state": "not-found"} for job_id in missing]
        print(json.dumps(data, indent=2))
        return 0
    if not shown and not missing:
        print("no jobs")
    for job in shown:
        _print_job(job, tree, keys)
    for job_id in missing:
        print(f"job {job_id}: not found (pruned?)")
    return 0


def _print_job(job: jobs.Job, tree: str | None, keys: dict[str, str | None] | None) -> None:
    state = job.state
    when = f"started {_ago(job.started)}" if state == "running" else _ago(job.started)
    stale = "  stale" if tree is not None and state != "running" and tree != job.tree else ""
    print(f"job {job.id:<3} {state:<9} {job.commit_label:<14} {when}{stale}")
    width = max((len(g.name) for g in job.gates), default=0)
    for g in job.gates:
        gstate = job.effective(g)
        label = f"failed({g.code})" if gstate == "failed" else gstate
        notes = []
        if keys is not None and gstate == "running" and keys.get(g.name) != g.key:
            notes.append("worktree changed since start: will not be recorded")
        if keys is not None and gstate != "died" and jobs.stale(g, keys):
            notes.append("stale")
        if gstate not in (*jobs.OK, "running", "pending", "cancelled"):
            notes.append(f"log: {os.path.relpath(job.log)}")
        took = jobs.running_for(g.started, g.ended) if gstate != "pending" else ""
        line = f"  {g.name:<{width}}  {g.key[:8] or '-':<8}  {label:<11} {took:<6}  {'  '.join(notes)}"
        print(line.rstrip())


def _job_json(job: jobs.Job, tree: str | None, keys: dict[str, str | None] | None) -> dict[str, object]:
    state = job.state
    return {
        "id": job.id,
        "state": state,
        "pid": job.pid,
        "head": job.head,
        "tree": job.tree,
        "dirty": job.dirty,
        "base": job.base,
        "started": job.started,
        "code": job.code,
        "stale": tree is not None and state != "running" and tree != job.tree,
        "log": job.log,
        "gates": [
            {
                "name": g.name,
                "key": g.key,
                "state": job.effective(g),
                "code": g.code,
                "started": g.started,
                "ended": g.ended,
                "ref": g.ref,
                "load": g.load,
                "stale": keys is not None and jobs.stale(g, keys),
                "drift": keys is not None and job.effective(g) == "running" and keys.get(g.name) != g.key,
            }
            for g in job.gates
        ],
    }


def cmd_cancel(config: Config, args: argparse.Namespace) -> int:
    """Stop a running job. Its unfinished gates become `cancelled`."""
    del config
    (job,) = _jobs_named(None if args.job is None else [args.job])
    if job.state != "running":
        print(f"job {job.id} is not running ({job.state})")
        return 0
    jobs.cancel(job, _runner(args))
    print(f"cancelled job {job.id}")
    return 0


def _print_plan(p: gate.GatePlan) -> None:
    print(f"gate `{p.name}`: {p.status}")
    print(f"  base {p.effective_base[:7] if p.effective_base else '(none)'} ({p.how})")
    print(f"  why: {p.reason}")
    if p.status == "required":
        for argv in p.commands:
            print(f"  run: {shlex.join(argv)}")


def _print_debug(p: gate.GatePlan) -> None:
    """The comparator walk and the diff from the effective base, to stderr."""
    err = sys.stderr
    print(f"gate `{p.name}`: comparator walk, newest first", file=err)
    for step in p.trail:
        print(f"  {step.label} {step.key or '(no key)'} {step.where or 'miss'}", file=err)
        for run, ok in step.runs:
            print(f"    {_ran_on(run)} ({'accepted' if ok else 'ignored'})", file=err)
    print(f"gate `{p.name}`: effective base {p.effective_base or '(none)'} ({p.how})", file=err)
    print(f"gate `{p.name}`: changed in scope: {', '.join(p.changed) or '(none)'}", file=err)
    for token, items in p.lists.items():
        print(f"gate `{p.name}`: {token}: {','.join(items) or '(empty)'}", file=err)


def _ran_on(run: dict[str, object]) -> str:
    """Where, when and for how long a run ran: `ci github-actions push refs/heads/main job=e2e <time> 12.3s`."""
    runner = run.get("runner")
    runner = runner if isinstance(runner, dict) else {}
    if runner.get("kind") == "ci":
        parts = [runner.get(name) for name in ("kind", "provider", "event", "ref")]
        if runner.get("job"):
            parts.append(f"job={runner['job']}")
    else:
        where = "@".join(str(part) for part in (run.get("user"), runner.get("host")) if part)
        parts = [runner.get("kind", "local"), where]
    duration = run.get("duration")
    # Records come from origin, so a hand-edited `duration` must not crash `--debug`.
    ok = isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration)
    took = jobs.took(float(duration)) if ok else None
    return " ".join(str(part) for part in [*parts, run.get("time"), took] if part)


def _run_commands(runner: Runner, spec: GateSpec, commands: list[list[str]]) -> int:
    """Run each command in turn. Returns the first non-zero exit code, or 0."""
    env = {**os.environ, **spec.env}
    for argv in commands:
        result = runner.run(argv, capture=False, env=env)
        if not result.ok:
            return result.returncode
    return 0


def cmd_verified(config: Config, args: argparse.Namespace) -> int:
    """Print `verified`/`unverified` AND set the exit code, as `image exists` does.

    A group is verified only when every member is.
    """
    names = gate.select(config, args.name)
    tree = gate.snapshot(args.head).tree
    # One exact-ref lookup for one gate. A group lists origin's gate refs once for all its members.
    origin = gate.OriginRecords() if len(names) > 1 else None
    if all(gate.verified(name, gate.key(config, name, tree), origin, args.accept) for name in names):
        print("verified")
        return 0
    print("unverified")
    return 1


def cmd_sync(config: Config, args: argparse.Namespace) -> int:
    del args
    _print_synced(gate.sync(config.gate_prune_after_days))
    return 0


def _print_synced(synced: gate.Synced, *, quiet: bool = False) -> None:
    """The counts, or that nothing moved. `quiet` prints nothing when nothing moved."""
    if synced.pushed or synced.pulled or synced.merged or synced.pruned:
        print(
            f"synced gate records with {gate.REMOTE}: pushed {synced.pushed}, pulled {synced.pulled}, "
            f"merged {synced.merged}, pruned {synced.pruned}"
        )
    elif not quiet:
        print(f"gate records already in sync with {gate.REMOTE}")


def cmd_github_outputs(config: Config, args: argparse.Namespace) -> int:
    """Emit `<gate>-status`, `-run`, `-verified` and `-key` for every gate, then the first three for every group.

    `-run` is `true` when the status is `required`, for a plain `if:`. A `.`
    in a name becomes `-`. `--summary` also writes a gate table to
    `$GITHUB_STEP_SUMMARY`, or to stdout when that is unset.
    """
    # One read of origin for all gates, not one per gate. `--full` reads none.
    origin = gate.OriginRecords()
    snap = gate.snapshot(args.head)
    statuses = {
        name: gate.plan(config, name, args.base, snap, origin, full=args.full, accept=args.accept)
        for name in sorted(config.gates)
    }
    pairs: dict[str, str] = {}
    for name, p in statuses.items():
        _add_status(pairs, gate_output_name(name), p.status)
        pairs[f"{gate_output_name(name)}-key"] = p.key
    for group, members in sorted(gate_groups(config).items()):
        _add_status(pairs, gate_output_name(group), _group_status([statuses[m].status for m in members]))
    emit_outputs(pairs)
    if args.summary:
        table = _summary_table(config, statuses, args.accept)
        if not write_summary(table):
            print(table, end="")
    return 0


def _summary_table(config: Config, statuses: dict[str, gate.GatePlan], accept: list[gate.Accept]) -> str:
    """A markdown table of every gate in config order, each group's row before its members'."""
    lines = ["## Gates", ""]
    if accept:
        lines += [f"Only runs accepted by {' or '.join(f'`--accept {a}`' for a in accept)} count.", ""]
    lines += ["| Gate | Status | Recorded by |", "| --- | --- | --- |"]
    groups = gate_groups(config)
    for name, spec in config.gates.items():
        if spec.group and groups[spec.group][0] == name:
            status = _group_status([statuses[m].status for m in groups[spec.group]])
            lines.append(f"| `{spec.group}` (group) | {status} |  |")
        lines.append(f"| `{name}` | {statuses[name].status} | {_recorded_by(statuses[name])} |")
    lines += ["", "A `verified` or `not-needed` gate's job is skipped."]
    return "\n".join(lines) + "\n"


def _recorded_by(p: gate.GatePlan) -> str:
    """For a verified gate, the newest accepted run at its key: `ci · `abc1234` · [CI / job](url)`."""
    if p.status != "verified":
        return ""
    # The first step is the snapshot, whose record made the gate verified.
    runs = [run for run, ok in p.trail[0].runs if ok]
    if not runs:
        return ""
    run = max(runs, key=lambda r: str(r.get("time") or ""))
    runner = run.get("runner")
    runner = runner if isinstance(runner, dict) else {}
    head = run.get("head")
    parts = [str(runner.get("kind") or "local")]
    if isinstance(head, str) and head:
        parts.append(f"`{head[:7]}`")
    # Records come from origin, and a workflow or job name is free text, so neither may break the row.
    if runner.get("kind") == "ci":
        where = _cell(" / ".join(str(runner[name]) for name in ("workflow", "job") if runner.get(name)))
        url = runner.get("url")
        if where and url:
            text = where.replace("[", "\\[").replace("]", "\\]")
            where = f"[{text}]({_cell(str(url))})"
    else:
        where = _cell(str(runner.get("host") or ""))
    if where:
        parts.append(where)
    return " · ".join(parts)


def _cell(text: str) -> str:
    """`text` safe inside a markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def _add_status(pairs: dict[str, str], prefix: str, status: str) -> None:
    pairs[f"{prefix}-status"] = status
    pairs[f"{prefix}-run"] = "true" if status == "required" else "false"
    pairs[f"{prefix}-verified"] = "true" if status == "verified" else "false"


def _group_status(statuses: list[str]) -> str:
    """`required` if any member is, else `verified` if every member is, else `not-needed`."""
    if "required" in statuses:
        return "required"
    if all(status == "verified" for status in statuses):
        return "verified"
    return "not-needed"


def _timeout(value: str) -> int | None:
    """Seconds to wait, 0 or more, or `none` to wait until the job is done."""
    if value == "none":
        return None
    seconds = int(value)
    if seconds < 0:
        raise argparse.ArgumentTypeError("must be 0 or more, or `none`")
    return seconds


def _job_ids(value: str) -> list[int]:
    """A comma-separated list of job numbers."""
    try:
        return [int(part) for part in value.split(",")]
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"`{value}` is not a comma-separated list of job numbers") from e


def _since(value: str) -> timedelta:
    """An age as a number and a unit: `90s`, `30m`, `8h` or `2d`."""
    m = re.fullmatch(r"(\d+)([smhd])", value)
    if not m:
        raise argparse.ArgumentTypeError("use a number and a unit, as in `30m`, `8h` or `2d`")
    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
    return timedelta(**{unit: int(m.group(1))})


def _add_timeout(p: argparse.ArgumentParser) -> None:
    # Hidden: `--wait` and `gate wait` take the hour. It still parses, for scripts that pass it, and for tests.
    _ = p.add_argument("--timeout", type=_timeout, default=_UNSET, help=argparse.SUPPRESS)


def _accept(value: str) -> gate.Accept:
    try:
        return gate.Accept.parse(value)
    except gate.GateError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _add_accept(p: argparse.ArgumentParser) -> None:
    _ = p.add_argument(
        "--accept",
        action="append",
        type=_accept,
        default=[],
        metavar="RAN_ON",
        help="count only runs on this runner: `ci`, `local`, or `field=value,...` over "
        f"{', '.join(gate.ACCEPT_FIELDS)}; repeat to accept any of several",
    )


def _add_head_arg(p: argparse.ArgumentParser) -> None:
    _ = p.add_argument("--head", default=None, help="key this commit, not the working tree")


def add_parsers(sub: argparse._SubParsersAction) -> None:
    gp = sub.add_parser("gate", help="record a local gate pass, and reuse it in CI")
    gsub = gp.add_subparsers(dest="cmd", required=True)

    lp = gsub.add_parser("list", help="list every gate and group")
    lp.set_defaults(func=cmd_list)

    kp = gsub.add_parser("key", help="print a gate's content key; a group, or no name, prints `<name> <key>` per gate")
    _ = kp.add_argument("name", nargs="?", help="a gate or a group; omit for every gate")
    # No `--base`: the key reads content only.
    _add_head_arg(kp)
    kp.set_defaults(func=cmd_key)

    rp = gsub.add_parser(
        "run", help="run gates on the working tree, unless the diff from the last verified commit does not need them"
    )
    _ = rp.add_argument("name", nargs="*", help="a gate, or a group for every gate in it")
    _ = rp.add_argument("--all", action="store_true", help="run every configured gate, in config order")
    # No `--head`: the commands run against the checked-out tree, so the only
    # content a record can describe is the working tree.
    add_diff_args(rp, head=False)
    _ = rp.add_argument(
        "--read-only",
        action="store_true",
        help="reuse a record, but write none",
    )
    add_full(rp, "run with complete lists, as if every tag changed; reads no record and no diff")
    _ = rp.add_argument("--fail-fast", action="store_true", help="stop at the first failing gate")
    _ = rp.add_argument(
        "--dry-run", action="store_true", help="print each gate's status, base and commands; run and write nothing"
    )
    _ = rp.add_argument(
        "--debug", action="store_true", help="print the comparator walk and the diff it chose to stderr"
    )
    _add_accept(rp)
    _ = rp.add_argument(
        "--wait",
        action="store_true",
        help="wait for the job, up to an hour, and exit with the gates' code; without it, return once it starts",
    )
    _add_timeout(rp)
    # The job process: run inline and report to this job directory.
    _ = rp.add_argument("--job-dir", default=None, help=argparse.SUPPRESS)
    rp.set_defaults(func=cmd_run)

    wp = gsub.add_parser(
        "wait", help="wait for a gate job; exits 0 passed, 1 failed, 2 usage or no such job, 3 still running"
    )
    _ = wp.add_argument("name", nargs="*", help="wait only for these gates or groups")
    _ = wp.add_argument("--job", type=_job_ids, default=None, metavar="N[,N...]", help="the jobs (default: the latest)")
    _add_timeout(wp)
    wp.set_defaults(func=cmd_wait)

    sp = gsub.add_parser("status", help="recent gate jobs, their gates, and which results are stale")
    _ = sp.add_argument(
        "--job", type=_job_ids, default=None, metavar="N[,N...]", help="exactly these jobs, whatever their age"
    )
    _ = sp.add_argument(
        "--since",
        type=_since,
        default=timedelta(hours=8),
        metavar="AGE",
        help="jobs started this recently (default: 8h)",
    )
    _ = sp.add_argument("--json", action="store_true", help="print the jobs as JSON, with full keys and record refs")
    sp.set_defaults(func=cmd_status)

    cp = gsub.add_parser("cancel", help="stop a running gate job")
    _ = cp.add_argument("--job", type=int, default=None, metavar="N", help="the job (default: the latest)")
    cp.set_defaults(func=cmd_cancel)

    vp = gsub.add_parser("verified", help="has this gate passed? prints verified/unverified, exits 0/1")
    _ = vp.add_argument("name", help="a gate, or a group, which is verified when every member is")
    _add_head_arg(vp)
    _add_accept(vp)
    vp.set_defaults(func=cmd_verified)

    sp = gsub.add_parser(
        "sync", help="sync gate records with origin: pull, merge runs, push, and prune expired records"
    )
    sp.set_defaults(func=cmd_sync)

    op = gsub.add_parser("github-outputs", help="emit <gate>-status, -run, -verified and -key as $GITHUB_OUTPUT lines")
    add_diff_args(op)
    _add_accept(op)
    add_full(op, "mark every gate and group required, as if every tag changed")
    _ = op.add_argument(
        "--summary", action="store_true", help="also write a gate table to $GITHUB_STEP_SUMMARY (or stdout)"
    )
    op.set_defaults(func=cmd_github_outputs)
