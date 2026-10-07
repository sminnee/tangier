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
import io
import json
import os
import re
import signal
import sys
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

from tangier import cli, gate, git, jobs, ranon
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

    `ignore_term` gives a job that only SIGKILL stops.
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
        f"job {job_id} is still running in the background: {what}.\n"
        f"Run `tangier gate wait --job {job_id}` to keep waiting (up to an hour; exits 0 if every gate passed, "
        "1 if one failed, 3 if still running).\n"
        "`tangier gate status` shows progress; `tangier gate cancel` stops it.\n"
        f"Full output: .git/tangier/jobs/{job_id}/output.log\n"
    )


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

    def tangier(self, *argv: str, runner: Any = None) -> tuple[int, str, str]:
        runner = runner if runner is not None else InProcessRunner()
        out, err = io.StringIO(), io.StringIO()
        with (
            contextlib.chdir(self.repo),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            mock.patch.object(gate_cmds, "clock", lambda: self.lag + sum(runner.slept)),
        ):
            code = cli.main(list(argv), runner=runner)
        return code, out.getvalue(), err.getvalue()

    def run_gates(self, *argv: str, runner: Any = None) -> tuple[int, str, str]:
        return self.tangier("gate", "run", *argv, "--base", "HEAD", "--full", runner=runner)

    def job(self, job_id: int = 1) -> jobs.Job:
        with contextlib.chdir(self.repo):
            found = jobs.find(job_id)
        assert found is not None
        return found

    def job_ids(self) -> list[int]:
        with contextlib.chdir(self.repo):
            return [j.id for j in jobs.all_jobs()]

    def report(self, job_id: int, name: str, state: str | None = None, code: int = 0) -> None:
        """Move one gate of a job along, as the job process would."""
        job = self.job(job_id)
        reporter = jobs.JobReporter(job.dir)
        with contextlib.chdir(self.repo):
            reporter.started(name, job.gate(name).key, reporter.offset())
            if state is not None:
                reporter.finished(name, state, code, line=f"gate `{name}`: {state}")

    def edit(self) -> None:
        with open(os.path.join(self.repo, "src/a.py"), "w") as fh:
            _ = fh.write("a = 2\n")

    def old_running_job(self, days: float) -> None:
        """A job that started `days` ago and is still running."""
        when = datetime.now(UTC) - timedelta(days=days)
        with mock.patch.object(gate, "now", return_value=when):
            _ = self.run_gates("lint", runner=SleeperRunner(self))

    def old_job(self, days: float) -> None:
        """A finished job that started `days` ago."""
        when = datetime.now(UTC) - timedelta(days=days)
        with contextlib.chdir(self.repo), mock.patch.object(gate, "now", return_value=when):
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
            r"gate `test`: failed in [\d.]+s \(exit 4\)\n  \| boom\n  \| at a\.py:1\n  log: \.git/tangier/jobs/1/output\.log\n$",
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
        self.assertTrue(out.endswith("  | last words\n  log: .git/tangier/jobs/1/output.log\n"), out)
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
        self.assertRegex(out, r"job 1 is still running in the background: test \([\d.]+s\)\.\nRun `tangier gate wait")

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
        progress = out.split("job 1 is still running")[0]
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
    def test_cancel_leaves_a_finished_job_alone(self) -> None:
        _ = self.run_gates("lint")
        with contextlib.chdir(self.repo):
            self.assertFalse(jobs.cancel(self.job(), RecordingRunner()))
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


if __name__ == "__main__":
    unittest.main()
