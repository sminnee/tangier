"""`tangier gate run`, `wait`, `status` and `cancel` as background jobs.

A real git repo throughout, as in `test_gate`. Most tests run the job process
in-process: `InProcessRunner.spawn` calls `cli.main` with the job's argv, its
output going to the job log, so the job is done before the wait starts.
`SleeperRunner` spawns a real `sleep` instead, for a job that stays running.
One test spawns a real job process end to end.
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

from tangier import cli, gate, jobs
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

    def tangier(self, *argv: str, runner: Any = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.chdir(self.repo), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), runner=runner if runner is not None else InProcessRunner())
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
            _ = self.run_gates("lint", "--timeout", "0", runner=SleeperRunner(self))

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
    def test_a_run_that_outlasts_the_timeout_exits_3_and_leaves_the_job_running(self) -> None:
        runner = SleeperRunner(self)
        code, out, _ = self.run_gates("lint", "--timeout", "5", runner=runner)
        self.assertEqual(code, 3)
        self.assertIn(
            "still running in background: starting. Run `tangier gate wait --job 1` or `tangier gate status`.", out
        )
        self.assertEqual(sum(runner.slept), 5)
        self.assertEqual(self.job().state, "running")

    # SPEC: gate#job-run-waits
    def test_ctrl_c_cancels_the_job(self) -> None:
        runner = SleeperRunner(self)
        # The wait's first sleep is interrupted. Cancel's own sleeps are not.
        with mock.patch.object(runner, "sleep", side_effect=[KeyboardInterrupt, *[None] * 20]):
            code, _, err = self.run_gates("lint", runner=runner)
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
    def test_a_second_run_is_refused_while_a_job_runs(self) -> None:
        _ = self.run_gates("lint", "--timeout", "0", runner=SleeperRunner(self))
        self.report(1, "lint")
        code, _, err = self.run_gates("lint")
        self.assertEqual(code, 2)
        self.assertIn("job 1 is running (lint). Run `tangier gate wait --job 1` or `tangier gate cancel`.", err)
        self.assertEqual(self.job_ids(), [1])

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
        _ = self.run_gates("--all", "--timeout", "0", runner=SleeperRunner(self))
        self.report(1, "lint", "passed")
        self.report(1, "test")
        self.assertEqual(self.tangier("gate", "wait", "lint", "--timeout", "0")[0], 0)
        code, out, _ = self.tangier("gate", "wait", "--timeout", "0")
        self.assertEqual(code, 3)
        self.assertRegex(out, r"still running in background: test \([\d.]+s\)\. Run `tangier gate wait --job 1`")

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
        _ = self.run_gates("lint", "--timeout", "0", runner=SleeperRunner(self))
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
        _ = self.run_gates("lint", "--timeout", "0", runner=SleeperRunner(self))
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
                        "stale": True,
                        "drift": False,
                    }
                ],
            },
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
        _ = self.run_gates("lint", "--timeout", "0", runner=SleeperRunner(self))
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


class TestCancel(JobCase):
    # SPEC: gate#cancel
    def test_cancel_stops_the_job_and_marks_its_gates(self) -> None:
        _ = self.run_gates("--all", "--timeout", "0", runner=SleeperRunner(self))
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
        _ = self.run_gates("lint", "--timeout", "0", runner=SleeperRunner(self, ignore_term=True))
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
