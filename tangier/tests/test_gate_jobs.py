"""`tangier gate run`, `wait`, `status` and `cancel` as background jobs.

A real git repo throughout, as in `test_gate`. Most tests run the job process
in-process: `InProcessRunner.spawn` calls `cli.main` with the job's argv, its
output going to the job log, so the job is done before the wait starts.
`SleeperRunner` spawns a real `sleep` instead, for a job that stays running.
One test spawns a real job process end to end.

The wait clock is patched: it reads the runner's `sleep` total, plus `lag`
for time a test spends outside a sleep.
"""

import contextlib
import getpass
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

from tangier import cli, gate, git, home, jobs, ranon
from tangier.commands import gate_cmds
from tangier.runner import Result, Subprocess
from tangier.tests.support import RecordingRunner, make_git_repo

CONFIG = """\
[inputs]
paths = ["src/**", "pipeline.toml"]

[gate.lint]
cmd = "bin/lint"
scope = ["inputs"]

[gate.test]
cmd = "bin/test"
scope = ["inputs"]
"""

FILES = {"pipeline.toml": CONFIG, "src/a.py": "a = 1\n"}


class InProcessRunner(RecordingRunner):
    """Runs the job process in this process, to the end, before `spawn` returns.

    `prints` maps a command to the output it prints, which lands in the job log.
    """

    def __init__(
        self, responses: dict[tuple[str, ...], Any] | None = None, prints: dict[str, str] | None = None
    ) -> None:
        super().__init__(responses)
        self.prints = prints or {}

    def spawn(
        self, argv: list[str], *, log: str, env: dict[str, str] | None = None, pass_fds: tuple[int, ...] = ()
    ) -> int:
        self.spawned.append(list(argv))
        # [python, -m, tangier, *args]
        with open(log, "a") as fh, contextlib.redirect_stdout(fh), contextlib.redirect_stderr(fh):
            _ = cli.main(argv[3:], runner=self)
        return 0

    def run(self, argv: list[str], **kwargs: Any) -> Result:
        if argv[0] in self.prints:
            print(self.prints[argv[0]])
        return super().run(argv, **kwargs)


class SleeperRunner(RecordingRunner):
    """Spawns a real process that holds the job's `alive` lock, as a job that runs until it is cancelled.

    It holds the slot `gate run` took for it, if one was free. Otherwise the job
    stays queued. `ignore_term` gives a job that only SIGKILL stops.
    """

    def __init__(self, testcase: unittest.TestCase, *, ignore_term: bool = False) -> None:
        super().__init__()
        self.testcase = testcase
        self.command = ["sh", "-c", "trap '' TERM; sleep 60"] if ignore_term else ["sleep", "60"]

    def spawn(
        self, argv: list[str], *, log: str, env: dict[str, str] | None = None, pass_fds: tuple[int, ...] = ()
    ) -> int:
        self.spawned.append(list(argv))
        pid = Subprocess().spawn(self.command, log=log, pass_fds=pass_fds)
        self.testcase.addCleanup(_reap, pid)
        return pid


def _reap(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(ChildProcessError):
        _ = os.waitpid(pid, 0)


def hint(job_id: int, what: str = "starting") -> str:
    """The wait hint for a job left running, as every path prints it."""
    return (
        f"job {job_id} is in the background: {what}.\n"
        f"Run `tangier gate wait --job {job_id}` to keep waiting (up to an hour; exits 0 if every gate passed, "
        "1 if one failed or it was cancelled, 3 if still queued or running).\n"
        "`tangier gate status` shows progress; `tangier gate cancel` stops it.\n"
        f"Full output: {log(job_id)}\n"
    )


def log(job_id: int) -> str:
    """A job's log path, as tangier prints it."""
    return home.tilde(os.path.join(home.root(), "jobs", str(job_id), "output.log"))


def _timeless(status: str) -> str:
    """`gate status` output with its times as `<ago>` and `<t>`, which a test cannot fix."""
    status = re.sub(r"\d+[smhd] ago", "<ago>", status)
    return re.sub(r"\b\d+(\.\d)?s\b|\b\d+m\d\ds\b", lambda m: "<t>".ljust(len(m.group())), status)


class JobCase(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = make_git_repo(self, FILES)
        patcher = mock.patch.dict(os.environ)
        _ = patcher.start()
        self.addCleanup(patcher.stop)
        for var in ("CI", "GITHUB_ACTIONS", "GITHUB_OUTPUT"):
            _ = os.environ.pop(var, None)
        home_dir = tempfile.TemporaryDirectory()
        self.addCleanup(home_dir.cleanup)
        self.home = os.environ["TANGIER_HOME"] = os.path.realpath(home_dir.name)
        self.lag = 0.0
        # A quiet machine, whatever this one's load: `TestLoad` patches its own.
        for name, value in (("getloadavg", (1.0, 1.0, 1.0)), ("cpu_count", 10)):
            patcher = mock.patch.object(os, name, return_value=value)
            _ = patcher.start()
            self.addCleanup(patcher.stop)
        # A stop signal that no handler catches records itself here, rather than ending the test run.
        self.uncaught: list[int] = []
        for sig in (signal.SIGTERM, signal.SIGHUP):
            self.addCleanup(signal.signal, sig, signal.signal(sig, lambda signum, _: self.uncaught.append(signum)))

    def tangier(self, *argv: str, runner: Any = None, cwd: str | None = None) -> tuple[int, str, str]:
        runner = runner if runner is not None else InProcessRunner()
        out, err = io.StringIO(), io.StringIO()
        with (
            contextlib.chdir(cwd or self.repo),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            mock.patch.object(gate_cmds, "clock", lambda: self.lag + sum(runner.slept)),
        ):
            code = cli.main(list(argv), runner=runner)
        return code, out.getvalue(), err.getvalue()

    def run_gates(self, *argv: str, runner: Any = None, cwd: str | None = None) -> tuple[int, str, str]:
        return self.tangier("gate", "run", *argv, "--base", "HEAD", "--full", runner=runner, cwd=cwd)

    def job(self, job_id: int = 1) -> jobs.Job:
        with contextlib.chdir(self.repo):
            found = jobs.find(job_id)
        assert found is not None
        return found

    def job_ids(self, cwd: str | None = None) -> list[int]:
        with contextlib.chdir(cwd or self.repo):
            return [j.id for j in jobs.all_jobs()]

    def report(self, job_id: int, name: str, state: str | None = None, code: int = 0) -> None:
        """Move one gate of a job along, as the job process would."""
        job = self.job(job_id)
        reporter = jobs.JobReporter(job.dir)
        with contextlib.chdir(self.repo):
            reporter.started(name, job.gate(name).key, reporter.offset())
            if state is not None:
                reporter.finished(name, state, code, line=f"gate `{name}`: {state}")

    def max_running(self, n: int) -> None:
        with open(os.path.join(self.home, "config.toml"), "w") as fh:
            _ = fh.write(f"[jobs]\nmax-running = {n}\n")

    def other_repo(self) -> str:
        return make_git_repo(self, FILES)

    def shown(self, repo: str) -> str:
        """A worktree as tangier prints it."""
        return home.tilde(os.path.realpath(repo))

    def edit(self) -> None:
        with open(os.path.join(self.repo, "src/a.py"), "w") as fh:
            _ = fh.write("a = 2\n")

    def old_running_job(self, days: float) -> None:
        """A job that started `days` ago and is still running."""
        when = datetime.now(UTC) - timedelta(days=days)
        with mock.patch.object(gate, "now", return_value=when):
            _ = self.run_gates("lint", runner=SleeperRunner(self))

    def old_job(self, days: float, cwd: str | None = None) -> None:
        """A finished job that started `days` ago."""
        when = datetime.now(UTC) - timedelta(days=days)
        with contextlib.chdir(cwd or self.repo), mock.patch.object(gate, "now", return_value=when):
            job = jobs.create(["gate", "run", "lint"], "HEAD", gate.snapshot(), {"lint": "k"})
            jobs.finish(job.dir, 0)


class TestRun(JobCase):
    # SPEC: gate#job-run-waits
    def test_a_short_run_prints_the_job_and_each_result(self) -> None:
        code, out, _ = self.run_gates("--all")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^job 1: lint, test \([0-9a-f]{7}\)$")
        self.assertIn("gate `lint`: passed in ", lines[1])
        self.assertIn("gate `test`: passed in ", lines[2])
        self.assertEqual([g.state for g in self.job().gates], ["passed", "passed"])

    # SPEC: gate#job-run-waits
    def test_the_job_reruns_the_same_command(self) -> None:
        runner = InProcessRunner()
        _ = self.run_gates("lint", runner=runner)
        argv = runner.spawned[0]
        self.assertEqual(argv[:3], [sys.executable, "-m", "tangier"])
        self.assertEqual(argv[3:], ["gate", "run", "--job-dir", self.job().dir, "lint", "--base", "HEAD", "--full"])

    # SPEC: gate#job-run-waits
    def test_a_failure_prints_its_output_tail_and_exits_with_its_code(self) -> None:
        runner = InProcessRunner(
            {("bin/test",): Result(4)}, prints={"bin/lint": "lint ok", "bin/test": "boom\nat a.py:1"}
        )
        code, out, _ = self.run_gates("--all", runner=runner)
        self.assertEqual(code, 4)
        # Only the failing gate's own output, without the result line it ends with.
        self.assertRegex(
            out,
            r"gate `test`: failed in [\d.]+s \(exit 4\)\n  \| boom\n  \| at a\.py:1\n  log: "
            + re.escape(log(1))
            + r"\n$",
        )
        self.assertEqual(self.job().gate("test").code, 4)

    # SPEC: gate#job-run-waits
    def test_fail_fast_cancels_the_gates_it_does_not_run(self) -> None:
        code, _, _ = self.run_gates("--all", "--fail-fast", runner=InProcessRunner({("bin/lint",): Result(4)}))
        self.assertEqual(code, 4)
        self.assertEqual([g.state for g in self.job().gates], ["failed", "cancelled"])
        self.assertEqual(self.job().gate("test").line, "gate `test`: not run (--fail-fast)")

    # SPEC: gate#job-run-waits
    def test_a_gate_that_exits_3_is_not_read_as_still_running(self) -> None:
        code, _, _ = self.run_gates("lint", runner=InProcessRunner({("bin/lint",): Result(3)}))
        self.assertEqual(code, 1)

    # SPEC: gate#job-run-waits
    # SPEC: gate#job-wait-hint
    def test_a_plain_run_returns_once_the_job_starts(self) -> None:
        runner = SleeperRunner(self)
        code, out, _ = self.run_gates("lint", runner=runner)
        self.assertEqual(code, 3)
        self.assertEqual(runner.slept, [])
        self.assertTrue(out.endswith(hint(1)), out)
        self.assertEqual(self.job().state, "running")

    # SPEC: gate#job-run-waits
    # SPEC: gate#job-wait-hint
    def test_run_wait_and_wait_stop_after_an_hour(self) -> None:
        # Ten-minute polls, so the hour is six of them.
        with mock.patch.object(gate_cmds, "POLL", 600):
            runner = SleeperRunner(self)
            code, out, _ = self.run_gates("lint", "--wait", runner=runner)
            self.assertEqual((code, sum(runner.slept)), (3, 3600))
            self.assertTrue(out.endswith(hint(1)), out)
            runner = RecordingRunner()
            code, out, _ = self.tangier("gate", "wait", runner=runner)
            self.assertEqual((code, sum(runner.slept)), (3, 3600))
            self.assertEqual(out, hint(1))
        self.assertEqual(self.job().state, "running")

    # SPEC: gate#job-run-waits
    def test_timeout_is_hidden_but_still_works(self) -> None:
        for command in ("run", "wait"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                _ = cli.main(["gate", command, "--help"])
            self.assertNotIn("--timeout", out.getvalue())
        runner = SleeperRunner(self)
        code, out, _ = self.run_gates("lint", "--timeout", "5", runner=runner)
        self.assertEqual((code, sum(runner.slept)), (3, 5))
        self.assertTrue(out.endswith(hint(1)), out)

    # SPEC: gate#job-run-waits
    def test_the_timeout_is_wall_clock_time(self) -> None:
        _ = self.run_gates("lint", runner=SleeperRunner(self))
        reload = gate_cmds._reload

        def slow(job: jobs.Job) -> jobs.Job:
            self.lag += 3
            return reload(job)

        runner = RecordingRunner()
        with mock.patch.object(gate_cmds, "_reload", slow):
            code, _, _ = self.tangier("gate", "wait", "--timeout", "5", runner=runner)
        # Reads at 3s and 7s, so one poll, not five.
        self.assertEqual((code, runner.slept), (3, [1]))

    def signalled(self, runner: Any, sig: int) -> Any:
        """`runner`, whose every sleep sends `sig` to this process, as a tool timeout or a closed shell would."""
        return mock.patch.object(runner, "sleep", side_effect=lambda _: os.kill(os.getpid(), sig))

    def assert_handlers_restored(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            os.kill(os.getpid(), sig)
        self.assertEqual(self.uncaught, [signal.SIGTERM, signal.SIGHUP])

    # SPEC: gate#job-wait-hint
    def test_sigterm_or_sighup_stops_wait_and_leaves_the_job_running(self) -> None:
        _ = self.run_gates("lint", runner=SleeperRunner(self))
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(sig=sig):
                runner = RecordingRunner()
                with self.signalled(runner, sig):
                    code, out, _ = self.tangier("gate", "wait", runner=runner)
                self.assertEqual((code, out), (3, "stopped waiting.\n" + hint(1)))
        self.assertEqual(self.job().state, "running")
        self.assert_handlers_restored()

    # SPEC: gate#job-wait-hint
    def test_sigterm_in_run_wait_leaves_its_own_job_running(self) -> None:
        runner = SleeperRunner(self)
        with self.signalled(runner, signal.SIGTERM):
            code, out, _ = self.run_gates("lint", "--wait", runner=runner)
        self.assertEqual(code, 3)
        self.assertTrue(out.endswith("stopped waiting.\n" + hint(1)), out)
        self.assertEqual(self.job().state, "running")
        self.assert_handlers_restored()

    # SPEC: gate#job-one-per-worktree
    # SPEC: gate#job-wait-hint
    def test_sigterm_while_queued_starts_no_job(self) -> None:
        _ = self.run_gates("lint", runner=SleeperRunner(self))
        runner = RecordingRunner()
        with self.signalled(runner, signal.SIGTERM):
            code, _, err = self.run_gates("test", "--wait", runner=runner)
        self.assertEqual(code, 2)
        self.assertEqual(err, "error: job 1 is running, so this run did not start.\n" + hint(1))
        self.assertEqual(self.job_ids(), [1])
        self.assert_handlers_restored()

    # SPEC: gate#job-run-waits
    def test_ctrl_c_cancels_the_job(self) -> None:
        code, _, err = self.run_gates("lint", "--wait", runner=InterruptedSleeper(self))
        self.assertEqual(code, 130)
        self.assertIn("cancelled job 1", err)
        self.assertEqual(self.job().state, "cancelled")
        # The caller knows why, so no notice.
        self.assertNotIn("was cancelled by", self.tangier("gate", "wait")[2])

    # SPEC: gate#job-inline-ci
    def test_ci_runs_inline(self) -> None:
        runner = InProcessRunner()
        with mock.patch.dict(os.environ, {"CI": "true"}):
            code, out, _ = self.run_gates("lint", runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.spawned, [])
        self.assertTrue(out.startswith("gate `lint`: passed"))
        self.assertEqual(self.job_ids(), [])

    # SPEC: gate#job-inline-ci
    def test_a_dry_run_runs_inline(self) -> None:
        runner = InProcessRunner()
        code, out, _ = self.tangier("gate", "run", "lint", "--dry-run", runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.spawned, [])
        self.assertIn("gate `lint`: required", out)

    # SPEC: gate#job-one-per-worktree
    # SPEC: gate#job-wait-hint
    def test_a_second_run_without_wait_is_refused_while_a_job_runs(self) -> None:
        _ = self.run_gates("lint", runner=SleeperRunner(self))
        self.report(1, "lint")
        code, out, err = self.run_gates("lint")
        self.assertEqual((code, out), (2, ""))
        self.assertRegex(err, r"^error: job 1 is running, so this run did not start\.\n")
        self.assertEqual(_timeless(err.split("\n", 1)[1]), _timeless(hint(1, "lint (0.0s)")))
        self.assertEqual(self.job_ids(), [1])

    # SPEC: gate#job-one-per-worktree
    def test_a_run_with_wait_queues_behind_the_running_job(self) -> None:
        _ = self.run_gates("lint", runner=SleeperRunner(self))
        first = self.job()

        class Queued(InProcessRunner):
            def sleep(inner, seconds: float) -> None:
                super().sleep(seconds)
                # The tree changes, then job 1 ends, while the second run waits for it.
                self.edit()
                _reap(first.pid)
                jobs.finish(first.dir, 0)

        runner = Queued()
        code, out, _ = self.run_gates("lint", "--wait", runner=runner)
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "job 1 is running: starting; waiting for it before starting")
        self.assertRegex(lines[1], r"^job 2: lint \([0-9a-f]{7}\+dirty\)$")
        self.assertIn("gate `lint`: passed in ", lines[2])
        self.assertEqual(len(lines), 3)
        self.assertEqual(runner.slept, [1])
        self.assertEqual(self.job(2).state, "passed")
        # Keyed when it started, not when it was queued.
        self.assertNotEqual(self.job(2).gate("lint").key, first.gate("lint").key)

    # SPEC: gate#job-one-per-worktree
    # SPEC: gate#job-wait-hint
    def test_a_queued_run_that_runs_out_of_time_starts_no_job(self) -> None:
        _ = self.run_gates("lint", runner=SleeperRunner(self))
        runner = RecordingRunner()
        code, out, err = self.run_gates("lint", "--timeout", "2", runner=runner)
        self.assertEqual((code, sum(runner.slept)), (2, 2))
        self.assertEqual(out, "job 1 is running: starting; waiting for it before starting\n")
        self.assertEqual(err, "error: job 1 is running, so this run did not start.\n" + hint(1))
        self.assertEqual(self.job_ids(), [1])

    # SPEC: gate#job-one-per-worktree
    def test_ctrl_c_while_queued_leaves_the_running_job_alone(self) -> None:
        _ = self.run_gates("lint", runner=SleeperRunner(self))
        runner = RecordingRunner()
        with mock.patch.object(runner, "sleep", side_effect=KeyboardInterrupt):
            code, _, err = self.run_gates("lint", "--wait", runner=runner)
        self.assertEqual(code, 130)
        self.assertIn("stopped waiting; this run did not start", err)
        self.assertEqual(self.job_ids(), [1])
        self.assertEqual(self.job().state, "running")

    # SPEC: gate#job-wait-hint
    def test_a_job_whose_gates_are_done_is_publishing(self) -> None:
        _ = self.run_gates("lint", runner=SleeperRunner(self))
        self.report(1, "lint", "passed")
        code, _, err = self.run_gates("lint")
        self.assertEqual(code, 2)
        self.assertTrue(err.endswith(hint(1, "publishing records")), err)

    # SPEC: gate#job-died
    def test_a_job_whose_process_is_gone_died(self) -> None:
        # `RecordingRunner.spawn` starts nothing.
        code, out, _ = self.run_gates("lint", runner=RecordingRunner())
        self.assertEqual(code, 1)
        self.assertIn("gate `lint`: died: the job process is gone", out)
        with open(self.job().log, "a") as fh:
            _ = fh.write("last words\n")
        _, out, _ = self.tangier("gate", "wait")
        self.assertTrue(out.endswith(f"  | last words\n  log: {log(1)}\n"), out)
        _, out, _ = self.tangier("gate", "status")
        self.assertRegex(out, r"job 1 +died")
        self.assertRegex(out, r"lint +[0-9a-f]{8} +died")


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


class TestWait(JobCase):
    # SPEC: gate#job-run-waits
    def test_a_terminal_sees_the_job_log(self) -> None:
        _ = self.run_gates("lint", runner=InProcessRunner({("bin/lint",): Result(4)}))
        out = Terminal()
        with contextlib.chdir(self.repo), contextlib.redirect_stdout(out):
            code = cli.main(["gate", "wait"], runner=RecordingRunner())
        self.assertEqual(code, 1)
        with open(self.job().log) as fh:
            self.assertEqual(out.getvalue(), fh.read())

    # SPEC: gate#wait-exit-codes
    def test_wait_reports_a_finished_job(self) -> None:
        _ = self.run_gates("--all", runner=InProcessRunner({("bin/test",): Result(4)}))
        code, out, _ = self.tangier("gate", "wait")
        self.assertEqual(code, 1)
        self.assertIn("gate `lint`: passed", out)
        self.assertRegex(out, r"gate `test`: failed in [\d.]+s \(exit 4\)")
        self.assertEqual(self.tangier("gate", "wait", "lint")[0], 0)

    # SPEC: gate#wait-exit-codes
    def test_wait_for_one_gate_returns_when_it_is_done(self) -> None:
        _ = self.run_gates("--all", runner=SleeperRunner(self))
        self.report(1, "lint", "passed")
        self.report(1, "test")
        self.assertEqual(self.tangier("gate", "wait", "lint", "--timeout", "0")[0], 0)
        code, out, _ = self.tangier("gate", "wait", "--timeout", "0")
        self.assertEqual(code, 3)
        self.assertRegex(out, r"job 1 is in the background: test \([\d.]+s\)\.\nRun `tangier gate wait")

    # SPEC: gate#job-run-waits
    def test_off_a_terminal_each_gate_says_when_it_starts(self) -> None:
        _ = self.run_gates("--all", runner=SleeperRunner(self))
        self.report(1, "lint")

        class Progress(RecordingRunner):
            def sleep(inner, seconds: float) -> None:
                super().sleep(seconds)
                if len(inner.slept) == 2:
                    self.report(1, "lint", "passed")
                    self.report(1, "test")

        code, out, _ = self.tangier("gate", "wait", "--timeout", "4", runner=Progress())
        self.assertEqual(code, 3)
        progress = out.split("job 1 is in the background")[0]
        self.assertEqual(progress, "gate `lint`: started\ngate `lint`: passed\ngate `test`: started\n")

    # SPEC: gate#wait-exit-codes
    def test_wait_with_no_job_exits_2(self) -> None:
        code, _, err = self.tangier("gate", "wait")
        self.assertEqual(code, 2)
        self.assertIn("no gate job", err)
        _ = self.run_gates("lint")
        self.assertEqual(self.tangier("gate", "wait", "--job", "1,9")[0], 2)

    # SPEC: gate#wait-exit-codes
    def test_wait_for_a_gate_not_in_the_job_exits_2(self) -> None:
        _ = self.run_gates("lint")
        code, _, err = self.tangier("gate", "wait", "test")
        self.assertEqual(code, 2)
        self.assertIn("gate test is not in job 1", err)

    # SPEC: gate#wait-exit-codes
    def test_ctrl_c_in_wait_leaves_the_job_running(self) -> None:
        _ = self.run_gates("lint", runner=SleeperRunner(self))
        runner = RecordingRunner()
        with mock.patch.object(runner, "sleep", side_effect=KeyboardInterrupt):
            code, _, err = self.tangier("gate", "wait", runner=runner)
        self.assertEqual(code, 130)
        self.assertIn("stopped waiting; the job carries on", err)
        self.assertEqual(self.job().state, "running")

    # SPEC: gate#wait-exit-codes
    def test_wait_on_several_jobs_exits_with_the_worst(self) -> None:
        _ = self.run_gates("lint")
        _ = self.run_gates("test", runner=InProcessRunner({("bin/test",): Result(4)}))
        self.assertEqual(self.tangier("gate", "wait", "--job", "1")[0], 0)
        self.assertEqual(self.tangier("gate", "wait", "--job", "1,2")[0], 1)

    # SPEC: gate#job-drift
    def test_wait_warns_when_the_tree_changes_under_a_running_gate(self) -> None:
        _ = self.run_gates("lint", runner=SleeperRunner(self))
        self.report(1, "lint")
        self.edit()
        code, _, err = self.tangier("gate", "wait", "--timeout", "0")
        self.assertEqual(code, 3)
        self.assertIn("gate `lint`: the working tree changed since it started", err)


class TestStatus(JobCase):
    # SPEC: gate#job-status
    def test_no_jobs(self) -> None:
        self.assertEqual(self.tangier("gate", "status"), (0, "no jobs\n", ""))

    # SPEC: gate#job-status
    def test_status_shows_each_job_and_gate_with_its_key(self) -> None:
        _ = self.run_gates("--all", runner=InProcessRunner({("bin/test",): Result(4)}))
        code, out, _ = self.tangier("gate", "status")
        self.assertEqual(code, 0)
        job = self.job()
        lines = out.splitlines()
        self.assertRegex(lines[0], rf"^job 1 +failed +{job.head[:7]} +\d+s ago$")
        self.assertRegex(lines[1], rf"^  lint +{job.gate('lint').key[:8]} +passed +[\d.]+s$")
        self.assertRegex(lines[2], rf"^  test +{job.gate('test').key[:8]} +failed\(4\) +[\d.]+s +log: .*output.log$")

    # SPEC: gate#job-status
    def test_status_lists_named_jobs_and_says_which_are_missing(self) -> None:
        _ = self.run_gates("lint")
        code, out, _ = self.tangier("gate", "status", "--job", "1,9")
        self.assertEqual(code, 0)
        self.assertIn("job 1 ", out)
        self.assertIn("job 9: not found (pruned?)", out)

    # SPEC: gate#job-status
    def test_status_shows_recent_jobs_and_always_the_latest(self) -> None:
        self.old_job(2)
        self.old_job(1)
        _, out, _ = self.tangier("gate", "status")
        self.assertIn("job 2 ", out)
        self.assertNotIn("job 1 ", out)
        _, out, _ = self.tangier("gate", "status", "--since", "3d")
        self.assertIn("job 1 ", out)

    # SPEC: gate#job-status
    def test_status_shows_a_running_job_whatever_its_age(self) -> None:
        self.old_job(1)
        self.old_running_job(2)
        _, out, _ = self.tangier("gate", "status")
        self.assertRegex(out, r"job 2 +running")
        self.assertNotIn("job 1 ", out)

    # SPEC: gate#job-status
    def test_status_json_has_full_keys_and_record_refs(self) -> None:
        with mock.patch.object(ranon, "load", return_value={"load": 2.5, "cpus": 8}):
            _ = self.run_gates("lint")
        self.edit()
        _, out, _ = self.tangier("gate", "status", "--json")
        (data,) = json.loads(out)
        job = self.job()
        key = job.gate("lint").key
        times = ("pid", "started", "ended")
        data["gates"] = [{k: v for k, v in g.items() if k not in times} for g in data["gates"]]
        self.assertEqual(
            {k: v for k, v in data.items() if k not in times},
            {
                "id": 1,
                "state": "passed",
                "head": job.head,
                "tree": job.tree,
                "dirty": False,
                "base": "HEAD",
                "code": 0,
                "stale": True,
                "log": job.log,
                "worktree": os.path.realpath(self.repo),
                "git_dir": os.path.realpath(os.path.join(self.repo, ".git")),
                "queue_position": None,
                "cancelled": None,
                "gates": [
                    {
                        "name": "lint",
                        "key": key,
                        "state": "passed",
                        "code": 0,
                        "ref": f"refs/tangier/gates/lint/{key}",
                        "load": {"load": 2.5, "cpus": 8},
                        "stale": True,
                        "drift": False,
                    }
                ],
            },
        )

    # SPEC: gate#failure-publishes
    def test_status_json_gives_a_failed_gate_its_failure_record(self) -> None:
        _ = self.run_gates("lint", runner=InProcessRunner({("bin/lint",): Result(3)}))
        _, out, _ = self.tangier("gate", "status", "--json")
        (data,) = json.loads(out)
        key = self.job().gate("lint").key
        self.assertEqual(
            [(g["state"], g["ref"]) for g in data["gates"]], [("failed", f"refs/tangier/failures/lint/{key}")]
        )

    # SPEC: gate#job-stale
    def test_a_result_is_stale_once_its_scope_changes(self) -> None:
        _ = self.run_gates("lint")
        _, out, _ = self.tangier("gate", "status")
        self.assertNotIn("stale", out)
        self.edit()
        _, out, _ = self.tangier("gate", "status")
        job = self.job()
        self.assertEqual(
            _timeless(out),
            f"job 1   passed    {job.head[:7]}        <ago>  stale\n  lint  {job.gate('lint').key[:8]}  passed      <t>     stale\n",
        )

    # SPEC: gate#job-drift
    def test_a_running_gate_warns_when_the_tree_changes(self) -> None:
        _ = self.run_gates("lint", runner=SleeperRunner(self))
        self.report(1, "lint")
        _, out, _ = self.tangier("gate", "status")
        self.assertNotIn("worktree changed", out)
        self.edit()
        _, out, _ = self.tangier("gate", "status")
        job = self.job()
        self.assertEqual(
            _timeless(out),
            f"job 1   running   {job.head[:7]}        started <ago>\n"
            f"  lint  {job.gate('lint').key[:8]}  running     <t>     worktree changed since start: will not be recorded\n",
        )

    # SPEC: gate#job-drift
    def test_a_gate_that_ran_on_a_changed_tree_is_unrecorded(self) -> None:
        class EditingRunner(InProcessRunner):
            def run(inner, argv: list[str], **kwargs: Any) -> Result:
                self.edit()
                return super().run(argv, **kwargs)

        code, out, _ = self.run_gates("lint", runner=EditingRunner())
        self.assertEqual(code, 1)
        self.assertIn("so no record was written", out)
        self.assertEqual(self.job().gate("lint").state, "unrecorded")


class RealRunner(Subprocess):
    """The real runner, which counts its sleeps for the wait clock."""

    def __init__(self) -> None:
        super().__init__()
        self.slept: list[float] = []


class RealSleep(RecordingRunner):
    """Sleeps for real, briefly, so a signalled process has time to exit."""

    def sleep(self, seconds: float) -> None:
        super().sleep(seconds)
        Subprocess().sleep(0.05)


class InterruptedSleeper(RealSleep, SleeperRunner):
    """A job that runs until cancelled, whose waiter is stopped by Ctrl-C at the first poll.

    Cancel's polls after that sleep for real, as in `RealSleep`.
    """

    def sleep(self, seconds: float) -> None:
        first = not self.slept
        super().sleep(seconds)
        if first:
            raise KeyboardInterrupt


class TestCancel(JobCase):
    # SPEC: gate#cancel
    def test_cancel_stops_the_job_and_marks_its_gates(self) -> None:
        _ = self.run_gates("--all", runner=SleeperRunner(self))
        self.report(1, "lint", "passed")
        self.report(1, "test")
        code, out, _ = self.tangier("gate", "cancel", runner=RealSleep())
        self.assertEqual((code, out), (0, "cancelled job 1\n"))
        job = self.job()
        self.assertEqual(job.state, "cancelled")
        self.assertEqual([g.state for g in job.gates], ["passed", "cancelled"])
        self.assertEqual(self.tangier("gate", "cancel")[1], "job 1 is not running (cancelled)\n")
        self.assertEqual(self.tangier("gate", "wait")[0], 1)

    # SPEC: gate#cancel
    def test_cancel_kills_a_job_that_ignores_sigterm(self) -> None:
        _ = self.run_gates("lint", runner=SleeperRunner(self, ignore_term=True))
        runner = RealSleep()
        self.assertEqual(self.tangier("gate", "cancel", runner=runner)[0], 0)
        self.assertEqual(sum(runner.slept) >= jobs.CANCEL_GRACE, True)
        self.assertEqual(self.job().state, "cancelled")

    # SPEC: gate#cancel
    def test_a_reap_during_a_cancel_does_not_read_the_job_as_died(self) -> None:
        _ = self.run_gates("lint", runner=SleeperRunner(self, ignore_term=True))
        stop = threading.Event()

        def reap_until_stopped() -> None:
            # Another command reaping throughout, as a waiter does on each poll.
            while not stop.is_set():
                jobs.reap(RecordingRunner())
                stop.wait(0.01)

        reaper = threading.Thread(target=reap_until_stopped)

        class StartsReaper(RealSleep):
            def sleep(inner, seconds: float) -> None:
                if not reaper.is_alive() and not stop.is_set():
                    reaper.start()
                super().sleep(seconds)

        try:
            self.assertEqual(self.tangier("gate", "cancel", runner=StartsReaper())[0], 0)
        finally:
            stop.set()
            reaper.join()
        self.assertEqual(self.job().state, "cancelled")
        self.assertFalse(os.path.exists(os.path.join(self.job().dir, "ended.json")))

    # SPEC: gate#cancel
    def test_other_commands_carry_on_during_a_cancel_and_see_the_job_stopping(self) -> None:
        _ = self.run_gates("lint", runner=SleeperRunner(self, ignore_term=True))
        seen: list[str] = []
        test = self

        class LooksMidCancel(RealSleep):
            def sleep(inner, seconds: float) -> None:
                super().sleep(seconds)
                if len(inner.slept) == 1:
                    # This would block, and the test hang, if the cancel held the jobs lock.
                    seen.append(test.tangier("gate", "status", runner=RecordingRunner())[1])

        self.assertEqual(self.tangier("gate", "cancel", runner=LooksMidCancel())[0], 0)
        self.assertRegex(seen[0], r"^job 1 +stopping ")
        self.assertEqual(self.job().state, "cancelled")

    # SPEC: gate#cancel
    def test_cancel_leaves_a_finished_job_alone(self) -> None:
        _ = self.run_gates("lint")
        with contextlib.chdir(self.repo):
            self.assertEqual(jobs.cancel([self.job()], RecordingRunner()), [])
        self.assertEqual(self.job().state, "passed")


class TestPrune(JobCase):
    # SPEC: gate#job-prune
    def test_a_new_job_prunes_old_jobs_but_keeps_the_latest_five(self) -> None:
        for _ in range(7):
            self.old_job(2)
        _ = self.run_gates("lint")
        self.assertEqual(self.job_ids(), [8, 7, 6, 5, 4])

    # SPEC: gate#job-prune
    def test_a_new_job_keeps_recent_jobs(self) -> None:
        for _ in range(7):
            self.old_job(0.5)
        _ = self.run_gates("lint")
        self.assertEqual(len(self.job_ids()), 8)

    # SPEC: gate#job-prune
    def test_a_new_job_prunes_the_oldest_past_the_log_limit(self) -> None:
        for _ in range(7):
            self.old_job(0.5)
            with open(self.job(len(self.job_ids())).log, "w") as fh:
                _ = fh.write("x" * 100)
        # 700 bytes of logs: deleting job 1 brings them under the limit.
        with mock.patch.object(jobs, "MAX_LOG_BYTES", 650):
            _ = self.run_gates("lint")
        self.assertEqual(self.job_ids(), [8, 7, 6, 5, 4, 3, 2])


class TestLoad(JobCase):
    def loaded(self, load: float) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        _ = stack.enter_context(mock.patch.object(os, "getloadavg", return_value=(load, 1.0, 1.0)))
        _ = stack.enter_context(mock.patch.object(os, "cpu_count", return_value=10))
        return stack

    # SPEC: gate#record-contents
    # SPEC: gate#run-load
    def test_a_pass_records_the_load_it_ran_under(self) -> None:
        with self.loaded(4.26):
            _ = self.run_gates("lint", "--wait")
        with contextlib.chdir(self.repo):
            (run,) = gate.read_runs(git.rev_parse_ref(self.job().gate("lint").ref or ""))
        self.assertEqual(run["load"], {"load": 4.3, "cpus": 10})
        self.assertEqual(self.job().gate("lint").load, {"load": 4.3, "cpus": 10})

    # SPEC: gate#run-load
    def test_a_load_above_the_cpu_count_warns_at_start_and_after_a_failure(self) -> None:
        runner = InProcessRunner({("bin/lint",): Result(4)}, prints={"bin/lint": "timed out"})
        with self.loaded(84.2):
            code, out, err = self.run_gates("lint", "--wait", runner=runner)
        self.assertEqual(code, 4)
        self.assertIn("warning: load average 84.2 on 10 CPUs; timeouts may come from load, not the code\n", err)
        self.assertIn("  | timed out\n", out)
        self.assertIn(
            "note: load average was 84.2 on 10 CPUs during this gate; if the failures are timeouts, rerun when "
            "load is lower\n",
            err,
        )

    # SPEC: gate#run-load
    def test_a_passing_gate_under_load_gets_the_warning_but_no_note(self) -> None:
        with self.loaded(84.2):
            _, _, err = self.run_gates("lint", "--wait")
        self.assertIn("warning: load average 84.2", err)
        self.assertNotIn("note:", err)

    # SPEC: gate#run-load
    def test_a_terminal_gets_the_note_once(self) -> None:
        with self.loaded(84.2):
            _ = self.run_gates("lint", runner=InProcessRunner({("bin/lint",): Result(4)}))
        err = io.StringIO()
        with contextlib.chdir(self.repo), contextlib.redirect_stdout(Terminal()), contextlib.redirect_stderr(err):
            _ = cli.main(["gate", "wait"], runner=RecordingRunner())
        self.assertEqual(err.getvalue().count("note: load average was 84.2 on 10 CPUs"), 1)

    # SPEC: gate#run-load
    def test_a_load_equal_to_the_cpu_count_says_nothing(self) -> None:
        with self.loaded(10.0):
            _, _, err = self.run_gates("lint", "--wait", runner=InProcessRunner({("bin/lint",): Result(4)}))
        self.assertNotIn("load average", err)

    # SPEC: gate#run-load
    def test_no_load_average_leaves_load_out_of_the_record(self) -> None:
        with mock.patch.object(os, "getloadavg", side_effect=OSError):
            _ = self.run_gates("lint", "--wait")
        with contextlib.chdir(self.repo):
            (run,) = gate.read_runs(git.rev_parse_ref(self.job().gate("lint").ref or ""))
        self.assertNotIn("load", run)
        self.assertIsNone(self.job().gate("lint").load)


class TestEndToEnd(JobCase):
    # SPEC: gate#job-run-waits
    def test_a_real_job_process_runs_the_gate(self) -> None:
        with open(os.path.join(self.repo, "pipeline.toml"), "w") as fh:
            _ = fh.write(CONFIG.replace('cmd = "bin/lint"', "cmd = \"sh -c 'echo linted'\""))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.chdir(self.repo), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["gate", "run", "lint", "--base", "HEAD", "--full", "--timeout", "none"])
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        self.assertRegex(out.getvalue(), r"gate `lint`: passed in [\d.]+s, recorded as")
        job = self.job()
        self.assertEqual(job.state, "passed")
        with contextlib.chdir(self.repo):
            self.assertIn("linted", jobs.output(job, "lint"))


class TestQueue(JobCase):
    def setUp(self) -> None:
        super().setUp()
        self.max_running(1)
        self.other = self.other_repo()

    def claim(self, job_id: int) -> int | None:
        with jobs.lock():
            return jobs.claim_free(self.job(job_id).dir, 1)

    def end(self, job_id: int) -> None:
        """Job `job_id` ends: its process goes, and its result is written."""
        _reap(self.job(job_id).pid)
        jobs.finish(self.job(job_id).dir, 0)

    # SPEC: gate#job-queue
    # SPEC: gate#job-wait-hint
    def test_a_job_over_the_limit_queues_until_a_slot_frees(self) -> None:
        _ = self.run_gates("lint", runner=SleeperRunner(self))
        code, out, _ = self.run_gates("lint", runner=SleeperRunner(self), cwd=self.other)
        self.assertEqual(code, 3)
        self.assertTrue(out.endswith(hint(2, "queued (1 of 1; 1 of 1 slots in use)")), out)
        self.assertEqual((self.job(1).state, self.job(2).state), ("running", "queued"))
        self.assertIsNone(self.claim(2))
        self.end(1)
        slot = self.claim(2)
        self.assertIsNotNone(slot)
        self.addCleanup(os.close, slot or 0)
        self.assertEqual(self.job(2).state, "running")

    # SPEC: gate#job-queue
    def test_the_queue_is_first_in_first_out(self) -> None:
        third = self.other_repo()
        for repo in (self.repo, self.other, third):
            _ = self.run_gates("lint", runner=SleeperRunner(self), cwd=repo)
        self.end(1)
        self.assertIsNone(self.claim(3))
        slot = self.claim(2)
        self.assertIsNotNone(slot)
        self.assertIsNone(self.claim(3))
        os.close(slot or 0)
        slot = self.claim(3)
        self.assertIsNotNone(slot)
        os.close(slot or 0)

    # SPEC: gate#job-queue
    # SPEC: gate#job-reap
    def test_a_job_that_dies_frees_its_slot_and_its_place(self) -> None:
        third = self.other_repo()
        for repo in (self.repo, self.other, third):
            _ = self.run_gates("lint", runner=SleeperRunner(self), cwd=repo)
        running, queued = self.job(1), self.job(2)

        class Kills(RecordingRunner):
            def sleep(inner, seconds: float) -> None:
                super().sleep(seconds)
                _reap(running.pid)
                _reap(queued.pid)

        runner = Kills()
        slot = jobs.claim_slot(self.job(3).dir, runner, 1)
        os.close(slot)
        self.assertEqual(runner.slept, [jobs.QUEUE_POLL])
        for job_id in (1, 2):
            self.assertEqual(self.job(job_id).state, "died")
            self.assertTrue(os.path.exists(os.path.join(self.job(job_id).dir, "ended.json")))

    # SPEC: gate#job-queue
    def test_a_waiter_says_where_its_job_is_in_the_queue(self) -> None:
        _ = self.run_gates("lint", runner=SleeperRunner(self))
        _ = self.run_gates("lint", runner=SleeperRunner(self), cwd=self.other)
        code, out, _ = self.tangier("gate", "wait", "--job", "2", "--timeout", "0", runner=RecordingRunner())
        self.assertEqual(code, 3)
        self.assertTrue(out.startswith("job 2 is queued: 1 of 1\n"), out)

    # SPEC: gate#job-queue
    def test_a_waiter_says_again_when_its_place_moves(self) -> None:
        third = self.other_repo()
        for repo in (self.repo, self.other, third):
            _ = self.run_gates("lint", runner=SleeperRunner(self), cwd=repo)
        test = self

        class EndsTheFirst(RecordingRunner):
            def sleep(inner, seconds: float) -> None:
                super().sleep(seconds)
                if len(inner.slept) == 1:
                    # Job 1 ends, and job 2 takes its slot, as its job process would.
                    test.end(1)
                    test.addCleanup(os.close, test.claim(2) or 0)

        _, out, _ = self.tangier("gate", "wait", "--job", "3", "--timeout", "3", runner=EndsTheFirst())
        self.assertEqual(
            [line for line in out.splitlines() if " is queued: " in line],
            ["job 3 is queued: 2 of 2", "job 3 is queued: 1 of 1"],
        )

    # SPEC: gate#job-queue
    # SPEC: gate#job-one-per-worktree
    def test_a_queued_job_is_its_worktrees_one_job(self) -> None:
        _ = self.run_gates("lint", runner=SleeperRunner(self), cwd=self.other)
        _ = self.run_gates("lint", runner=SleeperRunner(self))
        self.assertEqual(self.job(2).state, "queued")
        code, _, err = self.run_gates("test")
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error: job 2 is running, so this run did not start.\n"), err)
        self.assertEqual(self.job_ids(), [2])

    # SPEC: gate#status-all
    def test_status_all_shows_every_worktree_and_running_only_the_unfinished(self) -> None:
        self.old_job(0.1, cwd=self.other)
        _ = self.run_gates("lint", runner=SleeperRunner(self))
        _ = self.run_gates("lint", runner=SleeperRunner(self), cwd=self.other)
        _, out, _ = self.tangier("gate", "status", "--running", "--all")
        lines = out.splitlines()
        self.assertEqual(lines[0], "slots: 1 of 1 in use, 1 queued")
        self.assertRegex(lines[1], rf"^job 3 +queued .*  queue 1 of 1  {re.escape(self.shown(self.other))}$")
        self.assertRegex(lines[3], rf"^job 2 +running .*  {re.escape(self.shown(self.repo))}$")
        self.assertNotIn("job 1 ", out)
        _, out, _ = self.tangier("gate", "status")
        self.assertEqual([line.split()[1] for line in out.splitlines() if line.startswith("job")], ["2"])
        _, out, _ = self.tangier("gate", "status", "--all", "--json")
        fields = ("id", "state", "worktree", "git_dir", "queue_position", "stale")
        self.assertEqual(
            [{**{k: job[k] for k in fields}, "drift": [g["drift"] for g in job["gates"]]} for job in json.loads(out)],
            [
                {
                    **self.where(self.other),
                    "id": 3,
                    "state": "queued",
                    "queue_position": 1,
                    "stale": None,
                    "drift": [None],
                },
                {
                    **self.where(self.repo),
                    "id": 2,
                    "state": "running",
                    "queue_position": None,
                    "stale": False,
                    "drift": [False],
                },
                {
                    **self.where(self.other),
                    "id": 1,
                    "state": "passed",
                    "queue_position": None,
                    "stale": None,
                    "drift": [None],
                },
            ],
        )

    def where(self, repo: str) -> dict[str, str]:
        return {"worktree": os.path.realpath(repo), "git_dir": os.path.realpath(os.path.join(repo, ".git"))}

    # SPEC: gate#cancel-all
    # SPEC: gate#cancel-notice
    def test_cancel_all_stops_every_job_in_one_grace_period_and_tells_the_waiters(self) -> None:
        self.max_running(4)
        _ = self.run_gates("lint", runner=SleeperRunner(self, ignore_term=True))
        self.max_running(1)
        _ = self.run_gates("lint", runner=SleeperRunner(self, ignore_term=True), cwd=self.other)
        self.assertEqual((self.job(1).state, self.job(2).state), ("running", "queued"))
        runner = RealSleep()
        code, out, _ = self.tangier("gate", "cancel", "--all", "--reason", "load test", runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(
            out,
            f"cancelled job 2 ({self.shown(self.other)})\ncancelled job 1 ({self.shown(self.repo)})\n",
        )
        self.assertLess(sum(runner.slept), 2 * jobs.CANCEL_GRACE)
        self.assertEqual((self.job(1).state, self.job(2).state), ("cancelled", "cancelled"))
        code, _, err = self.tangier("gate", "wait", "--job", "2")
        self.assertEqual(code, 1)
        self.assertRegex(
            err,
            r"job 2 was cancelled by `tangier gate cancel --all` \(reason: load test\) at \d\d:\d\d\. "
            r"It may have been cancelled for a reason, such as an overloaded machine\. Before you start it again, "
            r"check with the system administrator or your human user\. Or push the branch and let CI "
            r"\(GitHub Actions\) run the gates\.\n",
        )
        _, out, _ = self.tangier("gate", "status", "--job", "2", "--json")
        (cancelled,) = [job["cancelled"] for job in json.loads(out)]
        self.assertEqual(
            {**cancelled, "time": "<t>"},
            {"by": "tangier gate cancel --all", "reason": "load test", "user": getpass.getuser(), "time": "<t>"},
        )
        self.assertEqual(self.tangier("gate", "cancel", "--all")[1], "no job is queued or running\n")

    # SPEC: gate#job-home
    def test_an_unwritable_home_says_what_to_allow(self) -> None:
        os.chmod(self.home, 0o500)
        self.addCleanup(os.chmod, self.home, 0o700)
        code, _, err = self.run_gates("lint")
        self.assertEqual(code, 2)
        self.assertIn(f"error: cannot write {self.home}/jobs: ", err)
        self.assertIn("in a sandbox, add it to the write allowlist (or set TANGIER_HOME)", err)

    # SPEC: gate#job-home
    def test_a_job_id_is_global(self) -> None:
        _ = self.run_gates("lint", cwd=self.other)
        code, out, _ = self.tangier("gate", "wait", "--job", "1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.job_ids(), [])
        self.assertEqual(self.job_ids(self.other), [1])


class TestReap(JobCase):
    # SPEC: gate#job-reap
    def test_after_a_reboot_every_unfinished_job_died(self) -> None:
        self.max_running(1)
        other = self.other_repo()
        _ = self.run_gates("lint", runner=SleeperRunner(self))
        _ = self.run_gates("lint", runner=SleeperRunner(self), cwd=other)
        # A reboot frees every lock.
        _reap(self.job(1).pid)
        _reap(self.job(2).pid)
        _, out, _ = self.tangier("gate", "status", "--all", runner=RecordingRunner())
        self.assertRegex(out, r"job 2 +died")
        self.assertRegex(out, r"job 1 +died")
        for job_id in (1, 2):
            self.assertEqual(self.job(job_id).code, 1)
            with open(os.path.join(self.job(job_id).dir, "ended.json")) as fh:
                self.assertEqual(json.load(fh)["by"], "reaper")

    # SPEC: gate#job-reap
    def test_a_parent_that_dies_before_the_spawn_leaves_a_reaped_job(self) -> None:
        with contextlib.chdir(self.repo), jobs.lock():
            job = jobs.create(["gate", "run", "lint"], "HEAD", gate.snapshot(), {"lint": "k"})
            os.close(jobs.hold_alive(job))
        _, out, _ = self.tangier("gate", "status", runner=RecordingRunner())
        self.assertRegex(out, r"job 1 +died")
        self.assertEqual(self.job().gate("lint").state, "died")

    # SPEC: gate#cancel
    def test_cancel_waits_for_the_commands_the_job_ran(self) -> None:
        _ = self.run_gates("lint", runner=SleeperRunner(self))
        job = self.job()
        fd = jobs.hold_procs(job.dir)
        # A command the job ran, out of the signal's reach, which exits when its stdin closes.
        proc = subprocess.Popen(["sh", "-c", "read _"], stdin=subprocess.PIPE, pass_fds=(fd,), start_new_session=True)
        self.addCleanup(proc.wait)
        os.close(fd)

        class EndsTheCommand(RealSleep):
            def sleep(inner, seconds: float) -> None:
                super().sleep(seconds)
                if len(inner.slept) == 3 and proc.stdin is not None:
                    proc.stdin.close()

        runner = EndsTheCommand()
        with contextlib.chdir(self.repo):
            cancelled = jobs.cancel([job], runner)
        self.assertEqual([j.id for j in cancelled], [1])
        self.assertGreaterEqual(len(runner.slept), 3)
        self.assertFalse(jobs.procs(job.dir))
        self.assertEqual(self.job().state, "cancelled")

    # SPEC: gate#job-reap
    def test_the_commands_of_a_job_whose_process_died_are_killed(self) -> None:
        with open(os.path.join(self.repo, "pipeline.toml"), "w") as fh:
            _ = fh.write(CONFIG.replace('cmd = "bin/lint"', "cmd = \"sh -c 'echo ready; exec sleep 60'\""))
        code, _, _ = self.tangier("gate", "run", "lint", "--base", "HEAD", "--full", runner=RealRunner())
        self.assertEqual(code, 3)
        job = self.job()
        self.addCleanup(_reap, job.pid)
        waited = 0.0
        # The command has started, and holds `procs`, once it prints.
        while "ready" not in jobs.log_tail(self.job()) and waited < 30:
            Subprocess().sleep(0.1)
            waited += 0.1
        self.assertIn("ready", jobs.log_tail(self.job()))
        # Only the job process: its `sleep 60` lives on, holding `procs`.
        os.kill(job.pid, signal.SIGKILL)
        _ = os.waitpid(job.pid, 0)
        self.assertEqual(self.job().state, "orphaned")
        _, out, _ = self.tangier("gate", "status", runner=RealSleep())
        self.assertRegex(out, r"job 1 +died")
        self.assertFalse(jobs.procs(job.dir))
        self.assertTrue(os.path.exists(os.path.join(job.dir, "ended.json")))


class TestMigrate(JobCase):
    def legacy(self, old_id: int, *, done: bool = True) -> str:
        """A job as tangier kept it before `~/.tangier`, in the git directory."""
        path = os.path.join(self.repo, ".git", "tangier", "jobs", str(old_id))
        os.makedirs(path)
        job = {
            "id": old_id,
            "pid": 0,
            "argv": ["gate", "run", "lint"],
            "base": "HEAD",
            "head": "a" * 40,
            "tree": "b" * 40,
            "dirty": False,
            "started": datetime.now(UTC).isoformat(),
        }
        for name, text in (
            ("job.json", json.dumps(job)),
            ("gates.json", json.dumps([{"name": "lint", "key": f"k{old_id}", "state": "passed", "code": 0}])),
            ("output.log", f"legacy {old_id}\n"),
            *([("done", "0")] if done else []),
        ):
            with open(os.path.join(path, name), "w") as fh:
                _ = fh.write(text)
        return path

    # SPEC: gate#job-migrate
    def test_legacy_jobs_move_with_new_ids_once_finished(self) -> None:
        self.legacy(3)
        self.legacy(5)
        running = self.legacy(7, done=False)
        fd = jobs.hold_alive(jobs.load(running))
        _ = self.run_gates("lint")
        self.assertEqual(self.job_ids(), [3, 2, 1])
        self.assertEqual([self.job(i).gate("lint").key for i in (1, 2)], ["k3", "k5"])
        self.assertEqual(self.job(1).worktree, os.path.realpath(self.repo))
        self.assertTrue(os.path.isdir(running))
        os.close(fd)
        with open(os.path.join(running, "done"), "w") as fh:
            _ = fh.write("0")
        _ = self.run_gates("lint")
        self.assertEqual(self.job(4).gate("lint").key, "k7")
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".git", "tangier")))


class TestPruneAcrossWorktrees(JobCase):
    # SPEC: gate#job-prune
    def test_each_worktree_keeps_its_latest_five(self) -> None:
        other = self.other_repo()
        self.old_job(2, cwd=other)
        for _ in range(7):
            self.old_job(2)
        _ = self.run_gates("lint")
        self.assertEqual(self.job_ids(), [9, 8, 7, 6, 5])
        self.assertEqual(self.job_ids(other), [1])

    # SPEC: gate#job-prune
    def test_a_job_whose_worktree_is_gone_is_pruned(self) -> None:
        other = self.other_repo()
        self.old_job(0, cwd=other)
        shutil.rmtree(other)
        _ = self.run_gates("lint")
        self.assertEqual([j.id for j in jobs.all_jobs(worktree_only=False)], [2])


if __name__ == "__main__":
    unittest.main()
