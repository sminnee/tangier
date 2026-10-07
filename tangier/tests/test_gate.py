"""`tangier gate` tests, at four seams.

Key      `gate.key` against a real git repo.
Run      `cli.main` with a `RecordingRunner`, in a real git repo.
Store    a local bare repo as `origin`, and a second clone of it.
Buckets  a custom package through the `changemap` commands.

Git is real throughout: the key is a hash of `git ls-tree` output and the store
is git refs, so a faked git would prove nothing about either.
"""

import contextlib
import io
import itertools
import json
import os
import re
import socket
import subprocess
import tempfile
import unittest
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest import mock

import tangier
from tangier import cli, gate, git, jobs, ranon
from tangier.commands import gate_cmds
from tangier.runner import Result
from tangier.tests.support import RecordingRunner, make_git_repo, make_origin, parse_toml

CONFIG = """\
[svc]
paths = "svc/**"
sha = true
unittest_items = "svc"

[svc-files]
files = true
paths = "svc/**/*.py"

[inputs]
paths = ["bin/test", "pipeline.toml"]

[image.svc]
dockerfile = "svc/Dockerfile"

[gate.backend]
cmd = ["bin/test --dirs {unittest-items} --files {svc-files}", "bin/lint"]
env = { TEST_DB = "1" }
scope = ["svc", "inputs"]
"""

FILES = {
    "pipeline.toml": CONFIG,
    "svc/a.py": "a = 1\n",
    # In no diff: only `--full` puts it in `--files`.
    "svc/b.py": "b = 1\n",
    "bin/test": "#!/bin/sh\n",
    "docs/notes.md": "notes\n",
    ".gitignore": "*.log\n",
}

# The gate's commands when the diff selects nothing: each list is an empty argument.
NOTHING_SELECTED = [["bin/test", "--dirs", "", "--files", ""], ["bin/lint"]]
# The gate's commands for `GateCase`'s diff, which changes `svc/a.py`.
SELECTED = [["bin/test", "--dirs", "svc", "--files", "svc/a.py"], ["bin/lint"]]
# The gate's commands under `--full`: every item, and every file the file-set's globs match.
FULL = [["bin/test", "--dirs", "svc", "--files", "svc/a.py,svc/b.py"], ["bin/lint"]]


def _git(root: str, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", root, "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _write(root: str, rel: str, content: str) -> None:
    with open(os.path.join(root, rel), "w") as fh:
        _ = fh.write(content)


def _commit(root: str, rel: str, content: str) -> str:
    """Commit one changed file and return the new commit."""
    _write(root, rel, content)
    _ = _git(root, "add", "-A")
    _ = _git(root, "commit", "-qm", f"change {rel}")
    return _git(root, "rev-parse", "HEAD")


def _gate_refs(root: str) -> list[str]:
    return _git(root, "for-each-ref", "--format=%(refname)", "refs/tangier/gates").split()


def _runs(root: str, ref: str) -> list[dict[str, Any]]:
    return json.loads(_git(root, "cat-file", "blob", ref))["runs"]


def _record(key: str, *times: str) -> str:
    """A `backend` record blob holding one CI run at each of `times`."""
    runs = [{"time": time, "runner": {"kind": "ci"}} for time in times]
    return json.dumps({"format": 2, "gate": "backend", "key": key, "runs": runs})


# The environment of a GitHub Actions job on a push to main.
GITHUB_PUSH = {
    "CI": "true",
    "GITHUB_ACTIONS": "true",
    "GITHUB_EVENT_NAME": "push",
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_REPOSITORY": "org/repo",
    "GITHUB_WORKFLOW": "CI",
    "GITHUB_JOB": "backend",
    "GITHUB_RUN_ID": "123",
    "GITHUB_RUN_ATTEMPT": "1",
    "GITHUB_SERVER_URL": "https://github.com",
    "RUNNER_NAME": "GitHub Actions 7",
}
# Where a run in `GITHUB_PUSH` ran.
GITHUB_PUSH_RAN_ON = {
    "kind": "ci",
    "provider": "github-actions",
    "event": "push",
    "ref": "refs/heads/main",
    "repository": "org/repo",
    "workflow": "CI",
    "job": "backend",
    "run_id": "123",
    "run_attempt": "1",
    "url": "https://github.com/org/repo/actions/runs/123",
    "runner_name": "GitHub Actions 7",
}
LOCAL_RAN_ON = {"kind": "local", "host": socket.gethostname()}


def _taking(seconds: float) -> Any:
    """Patch the run clock so that each gate's commands take `seconds`."""
    return mock.patch.object(gate_cmds, "clock", side_effect=itertools.count(100.0, seconds).__next__)


def _summary(*rows: str, accept: str = "") -> str:
    """The `github-outputs --summary` table with these rows, and `accept` as its filter line."""
    filter_line = f"Only runs accepted by {accept} count.\n\n" if accept else ""
    return (
        f"## Gates\n\n{filter_line}| Gate | Status | Recorded by |\n| --- | --- | --- |\n"
        + "".join(f"{row}\n" for row in rows)
        + "\nA `verified` or `not-needed` gate's job is skipped.\n"
    )


def _put_blob(root: str, ref: str, content: str, *, remote: str | None = None) -> None:
    """Point `ref` at a blob of `content`, here or, with `remote`, on that remote."""
    blob = subprocess.run(
        ["git", "-C", root, "hash-object", "-w", "--stdin"], input=content, capture_output=True, text=True, check=True
    ).stdout.strip()
    args = ("push", "-q", "-f", remote, f"{blob}:{ref}") if remote else ("update-ref", ref, blob)
    _ = _git(root, *args)


class GateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = make_git_repo(self, FILES)
        _ = _git(self.repo, "config", "user.email", "dev@example.com")
        # The diff from `self.base` to HEAD touches the scope and selects `svc`, so the gate is needed.
        self.base = _git(self.repo, "rev-parse", "HEAD")
        _ = _commit(self.repo, "svc/a.py", "a = 2\n")
        # A runner's `$GITHUB_OUTPUT` and `$GITHUB_STEP_SUMMARY` must not receive the test's output.
        patcher = mock.patch.dict(os.environ)
        _ = patcher.start()
        self.addCleanup(patcher.stop)
        _ = os.environ.pop("GITHUB_OUTPUT", None)
        _ = os.environ.pop("GITHUB_STEP_SUMMARY", None)
        # A run on a CI runner would record a `ci` runner.
        for var in ("CI", "GITHUB_ACTIONS"):
            _ = os.environ.pop(var, None)
        # These tests read a run's own output, so `gate run` runs inline, as in CI.
        # `test_gate_jobs` covers the background job.
        background = mock.patch.object(gate_cmds, "_in_background", return_value=False)
        _ = background.start()
        self.addCleanup(background.stop)

    def tangier(self, *argv: str, runner: Any = None, cwd: str | None = None) -> tuple[int, str, str]:
        """Run the CLI in a repo. Returns (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.chdir(cwd or self.repo), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), runner=runner or RecordingRunner())
        return code, out.getvalue(), err.getvalue()

    def key(self, body: str = CONFIG, *, head: str = "HEAD") -> str:
        with contextlib.chdir(self.repo):
            return gate.key(parse_toml(body), "backend", head)

    def tree(self) -> str:
        """The working tree as a tree object, uncommitted changes included."""
        with contextlib.chdir(self.repo):
            return git.worktree_tree()

    def worktree_key(self, body: str = CONFIG) -> str:
        return self.key(body, head=self.tree())

    def clone_origin(self, origin: str) -> str:
        """A second clone of `origin`, removed after the test."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _ = subprocess.run(["git", "clone", "-q", origin, tmp.name], capture_output=True, check=True)
        return tmp.name

    def shallow_clone(self, depth: int) -> str:
        """A clone of this repo's checked-out branch with `depth` commits of history, removed after the test."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        branch = _git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        url = f"file://{self.repo}"
        _ = subprocess.run(
            ["git", "clone", "-q", f"--depth={depth}", "--branch", branch, url, tmp.name],
            capture_output=True,
            check=True,
        )
        return tmp.name


class TestKey(GateCase):
    # SPEC: gate#key-content-only
    def test_the_same_tree_in_a_new_commit_keeps_the_key(self) -> None:
        before = self.key()
        _ = _git(self.repo, "commit", "-q", "--allow-empty", "-m", "re-cut")
        self.assertEqual(self.key(), before)
        self.assertRegex(before, r"^[0-9a-f]{40}$")

    # SPEC: gate#key-content-only
    def test_a_change_outside_the_scope_keeps_the_key(self) -> None:
        before = self.key()
        _ = _commit(self.repo, "docs/notes.md", "more notes\n")
        self.assertEqual(self.key(), before)

    # SPEC: gate#key-content-only
    def test_a_change_inside_the_scope_moves_the_key(self) -> None:
        before = self.key()
        _ = _commit(self.repo, "bin/test", "#!/bin/sh\nexit 0\n")
        self.assertNotEqual(self.key(), before)

    # SPEC: gate#key-commands
    def test_the_key_does_not_depend_on_the_base(self) -> None:
        first = _git(self.repo, "rev-parse", "HEAD")
        _ = _commit(self.repo, "svc/a.py", "a = 3\n")
        # Same head, so the same tree. Against `first` the diff selects `svc`;
        # against HEAD it selects nothing. The key hashes the raw commands.
        with contextlib.chdir(self.repo), contextlib.redirect_stderr(io.StringIO()):
            cfg = parse_toml(CONFIG)
            snap = gate.snapshot("HEAD")
            plans = [gate.plan(cfg, "backend", base, snap) for base in (first, "HEAD")]
        self.assertNotEqual(plans[0].commands, plans[1].commands)
        self.assertEqual(plans[0].key, plans[1].key)

    # SPEC: gate#key-commands
    def test_a_different_raw_command_moves_the_key(self) -> None:
        self.assertNotEqual(self.key(CONFIG.replace('"bin/lint"', '"bin/lint --strict"')), self.key())

    # SPEC: gate#key-env
    def test_a_different_env_moves_the_key(self) -> None:
        self.assertNotEqual(self.key(CONFIG.replace('TEST_DB = "1"', 'TEST_DB = "2"')), self.key())

    # SPEC: gate#key-fails-closed
    def test_an_unresolvable_head_raises(self) -> None:
        with self.assertRaises(gate.GateError) as ctx:
            _ = self.key(head="no-such-ref")
        self.assertIn("no-such-ref", str(ctx.exception))

    # SPEC: gate#key-fails-closed
    def test_an_unresolvable_base_fails_a_placeholder_gate_with_no_record(self) -> None:
        # The lenient diff would read this as "nothing changed" and run the
        # gate with every list empty.
        code, _, err = self.tangier("gate", "run", "backend", "--base", "origin/main")
        self.assertEqual(code, 2)
        self.assertIn("Fetch `origin/main` with enough history to reach the merge base", err)
        self.assertNotIn("fetch-depth", err)
        self.assertEqual(_gate_refs(self.repo), [])

    # SPEC: gate#key-fails-closed
    def test_a_scope_entry_that_matches_no_tracked_file_raises(self) -> None:
        # `svc` still matches, so the scope as a whole is not empty.
        body = CONFIG.replace('"inputs"]', '"ghost"]') + '[ghost]\npaths = "ghost/**"\n'
        with self.assertRaises(gate.GateError) as ctx:
            _ = self.key(body)
        self.assertIn("`ghost` matches no tracked file", str(ctx.exception))

    # SPEC: gate#key-all
    def test_a_bare_key_names_the_gate_even_when_there_is_one(self) -> None:
        self.assertEqual(self.tangier("gate", "key")[:2], (0, f"backend {self.key()}\n"))

    # SPEC: gate#key-all
    def test_a_bare_key_prints_nothing_when_one_gate_fails_closed(self) -> None:
        ghost = '[gate.ghost]\ncmd = "true"\nscope = "ghost"\n[ghost]\npaths = "ghost/**"\n'
        _ = _commit(self.repo, "pipeline.toml", CONFIG + ghost)
        code, out, err = self.tangier("gate", "key")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("`ghost` matches no tracked file", err)


class TestWorkingTree(GateCase):
    def names(self, tree: str) -> list[str]:
        return _git(self.repo, "ls-tree", "-r", "--name-only", tree).split()

    # SPEC: gate#key-working-tree
    def test_a_clean_tree_is_heads_tree(self) -> None:
        self.assertEqual(self.tree(), _git(self.repo, "rev-parse", "HEAD^{tree}"))

    # SPEC: gate#key-working-tree
    def test_the_tree_holds_changes_and_untracked_files_but_not_ignored_files(self) -> None:
        _write(self.repo, "svc/a.py", "a = 3\n")
        # Staged, then edited again: the working tree wins over the index.
        _write(self.repo, "bin/test", "#!/bin/sh\nexit 1\n")
        _ = _git(self.repo, "add", "bin/test")
        _write(self.repo, "bin/test", "#!/bin/sh\nexit 0\n")
        os.remove(os.path.join(self.repo, "docs/notes.md"))
        _write(self.repo, "scratch.txt", "new\n")
        _write(self.repo, "debug.log", "ignored\n")
        status = _git(self.repo, "status", "--porcelain")
        index = _git(self.repo, "ls-files", "--stage")

        tree = self.tree()

        self.assertEqual(_git(self.repo, "show", f"{tree}:svc/a.py"), "a = 3")
        self.assertEqual(_git(self.repo, "show", f"{tree}:bin/test"), "#!/bin/sh\nexit 0")
        self.assertIn("scratch.txt", self.names(tree))
        self.assertNotIn("docs/notes.md", self.names(tree))
        self.assertNotIn("debug.log", self.names(tree))
        # The real index is not touched: staged and unstaged changes stay as they were.
        self.assertEqual(_git(self.repo, "status", "--porcelain"), status)
        self.assertEqual(_git(self.repo, "ls-files", "--stage"), index)

    # SPEC: gate#key-working-tree
    def test_a_dirty_tree_keys_as_the_commit_made_from_it(self) -> None:
        _write(self.repo, "svc/a.py", "a = 3\n")
        _write(self.repo, "svc/new.py", "new = 1\n")
        dirty = self.worktree_key()
        self.assertNotEqual(dirty, self.key())
        _ = _git(self.repo, "add", "-A")
        _ = _git(self.repo, "-c", "user.name=other", "-c", "user.email=o@o", "commit", "-qm", "any message")
        self.assertEqual(self.key(), dirty)

    # SPEC: gate#key-working-tree
    def test_a_linked_worktree_uses_its_own_index(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        linked = os.path.join(tmp.name, "linked")
        _ = _git(self.repo, "worktree", "add", "-q", "--detach", linked)
        _write(linked, "svc/a.py", "a = 3\n")
        index = _git(linked, "ls-files", "--stage")
        with contextlib.chdir(linked):
            tree = git.worktree_tree()
        self.assertEqual(_git(linked, "show", f"{tree}:svc/a.py"), "a = 3")
        self.assertEqual(_git(linked, "ls-files", "--stage"), index)
        # The main checkout is clean, and keys as its HEAD.
        self.assertEqual(self.tree(), _git(self.repo, "rev-parse", "HEAD^{tree}"))

    # SPEC: gate#key-working-tree
    def test_changes_inside_a_submodule_raise(self) -> None:
        sub = make_git_repo(self, {"lib.py": "x = 1\n"})
        _ = _git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", sub, "vendored")
        _ = _git(self.repo, "commit", "-qm", "add a submodule")
        self.assertEqual(self.tree(), _git(self.repo, "rev-parse", "HEAD^{tree}"))
        _write(self.repo, "vendored/lib.py", "x = 2\n")
        with contextlib.chdir(self.repo), self.assertRaises(gate.GateError) as ctx:
            _ = gate.snapshot()
        self.assertIn("submodule vendored", str(ctx.exception))


class TestAccept(GateCase):
    # SPEC: gate#accept-filter
    def test_a_run_counts_when_any_accept_value_matches_it(self) -> None:
        with mock.patch.dict(os.environ, GITHUB_PUSH):
            self.assertEqual(self.tangier("gate", "run", "backend", "--base", self.base)[0], 0)
        cases = [
            ((), "verified"),
            (("ci",), "verified"),
            (("local",), "unverified"),
            (("local", "ci"), "verified"),
            (("kind=ci,event=push",), "verified"),
            (("kind=ci,event=pull_request",), "unverified"),
        ]
        for values, expected in cases:
            with self.subTest(accept=values):
                flags = [arg for value in values for arg in ("--accept", value)]
                self.assertEqual(self.tangier("gate", "verified", "backend", *flags)[1], f"{expected}\n")

    # SPEC: gate#accept-filter
    def test_an_unknown_field_or_kind_exits_2(self) -> None:
        cases = [
            ("host=mbp", "`host` is not a runner field"),
            ("kind=robot", "`robot` is not a runner kind"),
            ("robot", "`robot` is not a runner kind"),
            ("event=", "names no value"),
        ]
        for value, message in cases:
            with self.subTest(value=value):
                err = io.StringIO()
                with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(err):
                    _ = cli.main(["gate", "verified", "backend", "--accept", value])
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn(message, err.getvalue())


class TestRanOn(unittest.TestCase):
    # SPEC: gate#runner-detect
    def test_a_dev_machine_is_local_with_its_host(self) -> None:
        self.assertEqual(ranon.detect({}), LOCAL_RAN_ON)
        self.assertEqual(ranon.detect({"CI": "false"})["kind"], "local")

    # SPEC: gate#runner-detect
    def test_github_actions_names_the_job(self) -> None:
        self.assertEqual(ranon.detect(GITHUB_PUSH), GITHUB_PUSH_RAN_ON)

    # SPEC: gate#runner-detect
    def test_another_ci_is_an_unknown_provider(self) -> None:
        self.assertEqual(ranon.detect({"CI": "1"}), {"kind": "ci", "provider": "unknown"})


class TestDuration(unittest.TestCase):
    # SPEC: gate#run-records-pass
    def test_a_minute_or_more_shows_minutes_and_seconds(self) -> None:
        self.assertEqual(jobs.took(59.9), "59.9s")
        self.assertEqual(jobs.took(60.0), "1m00s")


class DirtyingRunner(RecordingRunner):
    """A runner whose commands leave an untracked file behind."""

    def __init__(self, root: str) -> None:
        super().__init__()
        self.root = root

    def run(self, argv: list[str], **kwargs: Any) -> Result:
        _write(self.root, "coverage.out", "x")
        return super().run(argv, **kwargs)


class CommittingRunner(RecordingRunner):
    """A runner whose commands commit, so HEAD moves and the content does not."""

    def __init__(self, root: str) -> None:
        super().__init__()
        self.root = root

    def run(self, argv: list[str], **kwargs: Any) -> Result:
        _ = _git(self.root, "commit", "-q", "--allow-empty", "-m", "from the gate")
        return super().run(argv, **kwargs)


class TestRun(GateCase):
    def run_gate(self, *extra: str, runner: Any = None) -> tuple[int, str, str]:
        return self.tangier("gate", "run", "backend", "--base", self.base, *extra, runner=runner)

    # SPEC: gate#run-stops-at-first-failure
    def test_a_failing_command_stops_the_run_and_writes_no_record(self) -> None:
        runner = RecordingRunner({("bin/test",): Result(3)})
        with _taking(3.2):
            code, out, _ = self.run_gate(runner=runner)
        self.assertEqual(code, 3)
        self.assertIn("gate `backend`: failed in 3.2s (exit 3)", out)
        self.assertEqual(runner.calls, SELECTED[:1])
        self.assertEqual(_gate_refs(self.repo), [])

    # SPEC: gate#run-dirty-after
    def test_a_run_that_changes_the_tree_writes_no_record(self) -> None:
        code, _, err = self.run_gate(runner=DirtyingRunner(self.repo))
        self.assertEqual(code, 1)
        self.assertIn("the working tree changed during the run", err)
        self.assertIn("no record was written", err)
        self.assertEqual(_gate_refs(self.repo), [])

    # SPEC: gate#run-dirty-after
    def test_a_run_that_moves_head_over_the_same_content_writes_the_record(self) -> None:
        code, _, err = self.run_gate(runner=CommittingRunner(self.repo))
        self.assertEqual(code, 0, err)
        self.assertEqual(_gate_refs(self.repo), [f"refs/tangier/gates/backend/{self.key()}"])

    # SPEC: gate#placeholder-whole-token
    def test_placeholders_become_the_selected_lists(self) -> None:
        runner = RecordingRunner()
        code, _, _ = self.run_gate(runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [["bin/test", "--dirs", "svc", "--files", "svc/a.py"], ["bin/lint"]])

    # SPEC: gate#run-records-pass
    # SPEC: gate#record-contents
    # SPEC: gate#record-runs
    def test_a_pass_on_a_clean_tree_writes_the_record(self) -> None:
        runner = RecordingRunner()
        with (
            mock.patch.object(gate, "now", return_value=datetime(2026, 3, 1, 12, 0, tzinfo=UTC)),
            _taking(12.34),
        ):
            code, out, _ = self.run_gate(runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, SELECTED)
        self.assertEqual([env["TEST_DB"] for env in runner.envs], ["1", "1"])
        # The gate's env is added to the caller's, not a replacement for it.
        self.assertIn("PATH", runner.envs[0])

        key = self.key()
        self.assertIn(f"gate `backend`: passed in 12.3s, recorded as refs/tangier/gates/backend/{key} (local)", out)
        self.assertEqual(_gate_refs(self.repo), [f"refs/tangier/gates/backend/{key}"])
        record = json.loads(_git(self.repo, "cat-file", "blob", f"refs/tangier/gates/backend/{key}"))
        self.assertEqual(
            record,
            {
                "format": 2,
                "gate": "backend",
                "key": key,
                "runs": [
                    {
                        "head": _git(self.repo, "rev-parse", "HEAD"),
                        "tree": _git(self.repo, "rev-parse", "HEAD^{tree}"),
                        "dirty": False,
                        "base": self.base,
                        "user": "dev@example.com",
                        "time": "2026-03-01T12:00:00+00:00",
                        "duration": 12.3,
                        "tangier": tangier.__version__,
                        "commands": SELECTED,
                        "runner": LOCAL_RAN_ON,
                    }
                ],
            },
        )

    # SPEC: gate#run-debug
    def test_debug_shows_no_duration_for_a_run_without_a_readable_one(self) -> None:
        runs = [
            {"time": "2026-03-01T00:00:00+00:00", "runner": {"kind": "ci"}},
            {"time": "2026-03-02T00:00:00+00:00", "runner": {"kind": "ci"}, "duration": float("inf")},
        ]
        record = {"format": 2, "gate": "backend", "key": self.key(), "runs": runs}
        _put_blob(self.repo, f"refs/tangier/gates/backend/{self.key()}", json.dumps(record))
        code, _, err = self.run_gate("--dry-run", "--debug")
        self.assertEqual(code, 0)
        self.assertIn("    ci 2026-03-01T00:00:00+00:00 (accepted)\n", err)
        self.assertIn("    ci 2026-03-02T00:00:00+00:00 (accepted)\n", err)

    # SPEC: gate#record-runs
    def test_a_second_pass_at_the_same_key_adds_a_run(self) -> None:
        ref = f"refs/tangier/gates/backend/{self.key()}"
        with mock.patch.object(gate, "now", return_value=datetime(2026, 3, 1, tzinfo=UTC)):
            _ = self.run_gate()
        with (
            mock.patch.dict(os.environ, GITHUB_PUSH),
            mock.patch.object(gate, "now", return_value=datetime(2026, 3, 2, tzinfo=UTC)),
        ):
            code, out, _ = self.run_gate("--full")
        self.assertEqual(code, 0)
        self.assertIn(f"recorded as {ref} (ci)", out)
        self.assertEqual(
            [(run["time"], run["runner"]) for run in _runs(self.repo, ref)],
            [("2026-03-01T00:00:00+00:00", LOCAL_RAN_ON), ("2026-03-02T00:00:00+00:00", GITHUB_PUSH_RAN_ON)],
        )

    # SPEC: gate#record-runs
    def test_a_record_keeps_the_newest_runs_only(self) -> None:
        ref = f"refs/tangier/gates/backend/{self.key()}"
        for day in range(1, gate.MAX_RUNS + 3):
            with mock.patch.object(gate, "now", return_value=datetime(2026, 3, day, tzinfo=UTC)):
                _ = self.run_gate("--full")
        runs = _runs(self.repo, ref)
        self.assertEqual(len(runs), gate.MAX_RUNS)
        self.assertEqual(runs[0]["time"][:10], "2026-03-03")

    # SPEC: gate#record-legacy
    def test_a_legacy_record_counts_as_a_local_run(self) -> None:
        ref = f"refs/tangier/gates/backend/{self.key()}"
        legacy = {"gate": "backend", "key": self.key(), "head": "abc", "time": "2026-03-01T00:00:00+00:00"}
        _put_blob(self.repo, ref, json.dumps(legacy))
        self.assertEqual(self.tangier("gate", "verified", "backend", "--accept", "local")[:2], (0, "verified\n"))
        self.assertEqual(self.tangier("gate", "verified", "backend", "--accept", "ci")[:2], (1, "unverified\n"))
        # A new run joins it.
        with mock.patch.object(gate, "now", return_value=datetime(2026, 3, 2, tzinfo=UTC)):
            self.assertEqual(self.run_gate("--full")[0], 0)
        runs = _runs(self.repo, ref)
        self.assertEqual(runs[0], {"head": "abc", "time": "2026-03-01T00:00:00+00:00", "runner": {"kind": "local"}})
        self.assertEqual((runs[1]["time"], runs[1]["runner"]), ("2026-03-02T00:00:00+00:00", LOCAL_RAN_ON))
        self.assertEqual(len(runs), 2)

    # SPEC: gate#comparator-ignores-rejected
    def test_an_unreadable_local_record_is_a_miss_and_is_replaced(self) -> None:
        ref = f"refs/tangier/gates/backend/{self.key()}"
        _put_blob(self.repo, ref, json.dumps({"format": 2, "runs": "x"}))
        runner = RecordingRunner()
        code, _, err = self.run_gate(runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, SELECTED)
        self.assertIn(f"warning: {ref} counts as absent", err)
        self.assertIn(f"warning: replaced {ref}", err)
        self.assertEqual([run["runner"] for run in _runs(self.repo, ref)], [LOCAL_RAN_ON])

    # SPEC: gate#record-legacy
    def test_a_newer_record_format_is_a_miss(self) -> None:
        ref = f"refs/tangier/gates/backend/{self.key()}"
        _put_blob(self.repo, ref, json.dumps({"format": 3, "runs": []}))
        code, out, err = self.tangier("gate", "verified", "backend")
        self.assertEqual((code, out), (1, "unverified\n"))
        self.assertIn("record format 3 is newer", err)

    # SPEC: gate#run-reuses-record
    def test_a_verified_gate_runs_nothing(self) -> None:
        _ = self.run_gate()
        # A re-cut: new commit, same tree.
        _ = _git(self.repo, "commit", "-q", "--allow-empty", "-m", "re-cut")
        runner = RecordingRunner()
        code, out, _ = self.run_gate(runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [])
        self.assertIn("verified", out)

    # SPEC: gate#run-read-only
    def test_read_only_reuses_a_record_on_a_clean_tree(self) -> None:
        _ = self.run_gate()
        runner = RecordingRunner()
        code, out, _ = self.run_gate("--read-only", runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [])
        self.assertIn("verified", out)

    # SPEC: gate#run-read-only
    def test_read_only_on_a_dirty_tree_reuses_a_matching_record(self) -> None:
        _write(self.repo, "svc/a.py", "a = 3\n")
        _ = self.run_gate()
        runner = RecordingRunner()
        code, out, _ = self.run_gate("--read-only", runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [])
        self.assertIn("verified", out)

    # SPEC: gate#run-records-pass
    # SPEC: gate#record-contents
    def test_a_pass_on_a_dirty_tree_records_the_tree_it_tested(self) -> None:
        _write(self.repo, "svc/new.py", "new = 1\n")
        with (
            mock.patch.object(gate, "now", return_value=datetime(2026, 3, 1, 12, 0, tzinfo=UTC)),
            _taking(12.34),
        ):
            code, out, err = self.run_gate()
        self.assertEqual(code, 0)
        self.assertIn("keying the working tree", err)
        key = self.worktree_key()
        self.assertIn(f"gate `backend`: passed in 12.3s, recorded as refs/tangier/gates/backend/{key} (local)", out)
        self.assertEqual(_gate_refs(self.repo), [f"refs/tangier/gates/backend/{key}"])
        self.assertEqual(
            _runs(self.repo, f"refs/tangier/gates/backend/{key}"),
            [
                {
                    "head": _git(self.repo, "rev-parse", "HEAD"),
                    "tree": self.tree(),
                    "dirty": True,
                    "base": self.base,
                    "user": "dev@example.com",
                    "time": "2026-03-01T12:00:00+00:00",
                    "duration": 12.3,
                    "tangier": tangier.__version__,
                    # The lists hold the untracked file as well as the committed change.
                    "commands": [["bin/test", "--dirs", "svc", "--files", "svc/a.py,svc/new.py"], ["bin/lint"]],
                    "runner": {"kind": "local", "host": socket.gethostname()},
                }
            ],
        )

    # SPEC: gate#run-dirty-notice
    def test_a_dirty_tree_lists_its_uncommitted_files_in_the_scope(self) -> None:
        _write(self.repo, "svc/a.py", "a = 3\n")
        _write(self.repo, "svc/new.py", "new = 1\n")
        _write(self.repo, "docs/notes.md", "more notes\n")
        _, _, err = self.run_gate("--dry-run")
        self.assertIn(
            "gate `backend`: keying the working tree; uncommitted changes in scope: svc/a.py, svc/new.py", err
        )
        self.assertNotIn("docs/notes.md", err)

    # SPEC: gate#run-dirty-notice
    def test_a_dirty_tree_outside_the_scope_says_so(self) -> None:
        _write(self.repo, "docs/notes.md", "more notes\n")
        _, _, err = self.run_gate("--dry-run")
        self.assertIn("gate `backend`: keying the working tree; no uncommitted change touches the scope", err)

    # SPEC: gate#run-dirty-notice
    def test_a_clean_tree_prints_no_notice(self) -> None:
        _, _, err = self.run_gate("--dry-run")
        self.assertNotIn("working tree", err)

    # SPEC: gate#record-reused-after-commit
    def test_a_pass_on_a_dirty_tree_verifies_the_commit_made_from_it(self) -> None:
        _write(self.repo, "svc/a.py", "a = 3\n")
        _write(self.repo, "svc/new.py", "new = 1\n")
        _ = self.run_gate()
        _ = _git(self.repo, "add", "-A")
        _ = _git(self.repo, "-c", "user.name=other", "commit", "-qm", "the tested work, any message")
        runner = RecordingRunner()
        code, out, _ = self.run_gate(runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [])
        self.assertIn("verified", out)

    # SPEC: gate#record-reused-after-commit
    def test_a_commit_of_part_of_the_tested_work_runs_again(self) -> None:
        _write(self.repo, "svc/a.py", "a = 3\n")
        _write(self.repo, "svc/new.py", "new = 1\n")
        _ = self.run_gate()
        tested = self.worktree_key()
        _ = _git(self.repo, "add", "svc/a.py")
        _ = _git(self.repo, "commit", "-qm", "half the tested work")
        # Drop the rest, so the tree is exactly the partial commit.
        os.remove(os.path.join(self.repo, "svc/new.py"))
        runner = RecordingRunner()
        code, _, _ = self.run_gate(runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, SELECTED)
        self.assertEqual(_gate_refs(self.repo), sorted(f"refs/tangier/gates/backend/{k}" for k in (tested, self.key())))

    # SPEC: gate#cli-head-defaults
    def test_key_and_verified_read_the_working_tree(self) -> None:
        _ = self.run_gate()
        _write(self.repo, "svc/a.py", "a = 3\n")
        self.assertEqual(self.tangier("gate", "key", "backend")[1], f"{self.worktree_key()}\n")
        self.assertEqual(self.tangier("gate", "key", "backend", "--head", "HEAD")[1], f"{self.key()}\n")
        self.assertEqual(self.tangier("gate", "verified", "backend")[:2], (1, "unverified\n"))
        self.assertEqual(self.tangier("gate", "verified", "backend", "--head", "HEAD")[:2], (0, "verified\n"))

    # SPEC: gate#run-read-only
    def test_read_only_runs_on_a_miss_and_writes_no_record(self) -> None:
        runner = RecordingRunner()
        code, _, _ = self.run_gate("--read-only", runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, SELECTED)
        self.assertEqual(_gate_refs(self.repo), [])

    # SPEC: gate#run-read-only
    def test_read_only_passes_when_the_run_dirties_the_tree(self) -> None:
        code, _, _ = self.run_gate("--read-only", runner=DirtyingRunner(self.repo))
        self.assertEqual(code, 0)

    # SPEC: gate#run-full
    def test_full_runs_a_verified_gate_and_writes_the_record(self) -> None:
        ref = f"refs/tangier/gates/backend/{self.key()}"
        with mock.patch.object(gate, "now", return_value=datetime(2026, 3, 1, tzinfo=UTC)):
            _ = self.run_gate()
        first = _git(self.repo, "rev-parse", ref)
        runner = RecordingRunner()
        with mock.patch.object(gate, "now", return_value=datetime(2026, 3, 2, tzinfo=UTC)):
            code, _, _ = self.run_gate("--full", runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, FULL)
        # A new record, with the later time, replaces the first.
        self.assertNotEqual(_git(self.repo, "rev-parse", ref), first)

    # SPEC: gate#run-full
    def test_full_on_a_dirty_tree_runs_and_writes_the_record(self) -> None:
        # An untracked test file joins the complete file-set: `--full` reads the working tree.
        _write(self.repo, "svc/new.py", "new = 1\n")
        runner = RecordingRunner()
        code, _, _ = self.run_gate("--full", runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(
            runner.calls, [["bin/test", "--dirs", "svc", "--files", "svc/a.py,svc/b.py,svc/new.py"], ["bin/lint"]]
        )
        self.assertEqual(_gate_refs(self.repo), [f"refs/tangier/gates/backend/{self.worktree_key()}"])

    # SPEC: gate#run-read-only
    # SPEC: gate#run-full
    def test_read_only_with_full_runs_on_a_dirty_tree_and_writes_nothing(self) -> None:
        _write(self.repo, "scratch.txt", "dirty\n")
        runner = RecordingRunner()
        code, _, _ = self.run_gate("--read-only", "--full", runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, FULL)
        self.assertEqual(_gate_refs(self.repo), [])

    # SPEC: gate#run-read-only
    # SPEC: gate#run-full
    def test_read_only_with_full_ignores_an_existing_record(self) -> None:
        _ = self.run_gate()
        runner = RecordingRunner()
        _ = self.run_gate("--read-only", "--full", runner=runner)
        self.assertEqual(runner.calls, FULL)

    # SPEC: gate#unknown-gate
    def test_an_unknown_gate_is_an_error(self) -> None:
        code, _, err = self.tangier("gate", "key", "nope")
        self.assertEqual(code, 2)
        self.assertIn("backend", err)


class TestNeed(GateCase):
    """`gate run` against a diff that starts at `self.start`, after the test's commits."""

    NO_PLACEHOLDER = CONFIG.replace("bin/test --dirs {unittest-items} --files {svc-files}", "bin/test")

    def use_config(self, body: str) -> None:
        """Commit `body` as the config, and start the diff after it."""
        if body != CONFIG:
            _ = _commit(self.repo, "pipeline.toml", body)
        self.start = _git(self.repo, "rev-parse", "HEAD")

    def run_gate(self, *extra: str, base: str | None = None) -> tuple[int, str, str, RecordingRunner]:
        runner = RecordingRunner()
        code, out, err = self.tangier("gate", "run", "backend", "--base", base or self.start, *extra, runner=runner)
        return code, out, err, runner

    def assert_not_needed(self, *extra: str) -> None:
        code, out, _, runner = self.run_gate(*extra)
        self.assertEqual(code, 0)
        self.assertIn("gate `backend`: not-needed for this diff", out)
        self.assertEqual(runner.calls, [])
        self.assertEqual(_gate_refs(self.repo), [])

    # SPEC: gate#need-scope-touched
    # SPEC: gate#run-not-needed
    def test_a_change_outside_the_scope_is_not_needed(self) -> None:
        self.use_config(self.NO_PLACEHOLDER)
        _ = _commit(self.repo, "docs/notes.md", "more notes\n")
        self.assert_not_needed()

    # SPEC: gate#need-scope-touched
    def test_a_change_inside_the_scope_runs(self) -> None:
        self.use_config(self.NO_PLACEHOLDER)
        _ = _commit(self.repo, "bin/test", "#!/bin/sh\nexit 0\n")
        self.assertEqual(self.run_gate()[3].calls, [["bin/test"], ["bin/lint"]])

    # SPEC: gate#need-scope-touched
    def test_a_change_in_a_depends_tag_runs(self) -> None:
        body = self.NO_PLACEHOLDER.replace('unittest_items = "svc"', 'unittest_items = "svc"\ndepends = ["lib"]')
        self.use_config(body + '[lib]\npaths = "lib/**"\n')
        os.mkdir(os.path.join(self.repo, "lib"))
        _ = _commit(self.repo, "lib/util.py", "util = 1\n")
        self.assertEqual(self.run_gate()[3].calls, [["bin/test"], ["bin/lint"]])

    # SPEC: gate#need-key-inputs-only
    def test_a_change_only_sha_exclude_matches_is_not_needed(self) -> None:
        self.use_config(self.NO_PLACEHOLDER)
        _ = _commit(self.repo, "svc/README.md", "docs\n")
        self.assert_not_needed()

    # SPEC: gate#need-scope-touched
    def test_a_deleted_file_in_the_scope_runs(self) -> None:
        self.use_config(self.NO_PLACEHOLDER)
        _ = _git(self.repo, "rm", "-q", "bin/test")
        _ = _git(self.repo, "commit", "-qm", "drop bin/test")
        self.assertEqual(self.run_gate()[3].calls, [["bin/test"], ["bin/lint"]])

    # SPEC: gate#need-empty-placeholders
    def test_a_placeholder_gate_with_every_list_empty_is_not_needed(self) -> None:
        # `bin/test` is in the scope, but in no items tag and no file set.
        self.use_config(CONFIG)
        _ = _commit(self.repo, "bin/test", "#!/bin/sh\nexit 0\n")
        self.assert_not_needed()

    # SPEC: gate#need-empty-placeholders
    def test_a_placeholder_gate_with_one_list_selected_runs(self) -> None:
        # A non-Python file selects the `svc` items, but no `svc-files`.
        self.use_config(CONFIG)
        _ = _commit(self.repo, "svc/data.txt", "data\n")
        self.assertEqual(self.run_gate()[3].calls, [["bin/test", "--dirs", "svc", "--files", ""], ["bin/lint"]])

    # SPEC: gate#run-full
    def test_full_runs_a_gate_that_is_not_needed(self) -> None:
        self.use_config(self.NO_PLACEHOLDER)
        _ = _commit(self.repo, "docs/notes.md", "more notes\n")
        code, _, _, runner = self.run_gate("--full")
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [["bin/test"], ["bin/lint"]])
        self.assertEqual(len(_gate_refs(self.repo)), 1)

    # SPEC: gate#need-scope-touched
    def test_an_uncommitted_change_inside_the_scope_runs(self) -> None:
        self.use_config(self.NO_PLACEHOLDER)
        _write(self.repo, "bin/test", "#!/bin/sh\nexit 0\n")
        self.assertEqual(self.run_gate()[3].calls, [["bin/test"], ["bin/lint"]])

    # SPEC: gate#need-scope-touched
    # SPEC: gate#run-not-needed
    def test_an_untracked_file_outside_the_scope_is_not_needed(self) -> None:
        self.use_config(self.NO_PLACEHOLDER)
        _write(self.repo, "scratch.txt", "dirty\n")
        self.assert_not_needed()

    # SPEC: gate#run-reuses-record
    def test_an_uncommitted_change_outside_the_scope_keeps_heads_record(self) -> None:
        self.use_config(self.NO_PLACEHOLDER)
        _ = self.run_gate("--full")
        _write(self.repo, "docs/notes.md", "more notes\n")
        code, out, _, runner = self.run_gate()
        self.assertEqual((code, runner.calls), (0, []))
        self.assertIn("verified", out)

    # SPEC: gate#placeholder-whole-token
    def test_placeholder_lists_hold_uncommitted_and_untracked_files(self) -> None:
        self.use_config(CONFIG)
        _write(self.repo, "svc/a.py", "a = 3\n")
        _write(self.repo, "svc/new.py", "new = 1\n")
        self.assertEqual(
            self.run_gate()[3].calls, [["bin/test", "--dirs", "svc", "--files", "svc/a.py,svc/new.py"], ["bin/lint"]]
        )

    # SPEC: gate#need-unreadable-base-runs
    def test_an_unreadable_base_warns_and_runs(self) -> None:
        # A shallow CI checkout holds no `origin/main`.
        self.use_config(self.NO_PLACEHOLDER)
        code, _, err, runner = self.run_gate(base="origin/main")
        self.assertEqual(code, 0)
        self.assertIn("warning", err)
        self.assertIn("needed", err)
        self.assertNotIn("fetch-depth", err)
        self.assertEqual(runner.calls, [["bin/test"], ["bin/lint"]])

    # SPEC: gate#need-unreadable-base-runs
    def test_an_unreadable_base_in_a_shallow_clone_names_the_fix(self) -> None:
        self.use_config(self.NO_PLACEHOLDER)
        runner = RecordingRunner()
        clone = self.shallow_clone(1)
        code, _, err = self.tangier("gate", "run", "backend", "--base", "HEAD^1", runner=runner, cwd=clone)
        self.assertEqual((code, runner.calls), (0, [["bin/test"], ["bin/lint"]]))
        self.assertIn("so it counts as needed. This is a shallow clone", err)
        self.assertIn("`fetch-depth` of 2 or more with `--base HEAD^1`", err)


# `CONFIG` with a second item, `other`, in the scope.
TWO_ITEMS = CONFIG.replace('scope = ["svc", "inputs"]', 'scope = ["svc", "other", "inputs"]') + (
    '[other]\npaths = "other/**"\nunittest_items = "other"\n'
)
# The gate's commands when the diff selects only `other`.
OTHER_ONLY = [["bin/test", "--dirs", "other", "--files", ""], ["bin/lint"]]
# The gate's commands for the whole branch, from `self.base`.
BOTH = [["bin/test", "--dirs", "other,svc", "--files", "svc/a.py"], ["bin/lint"]]
# The gate's commands under `--full`.
FULL_TWO_ITEMS = [["bin/test", "--dirs", "other,svc", "--files", "svc/a.py,svc/b.py"], ["bin/lint"]]


class TestComparator(GateCase):
    """A branch off `self.base` that changes `svc`, then adds `other` to the config."""

    def setUp(self) -> None:
        super().setUp()
        os.mkdir(os.path.join(self.repo, "other"))
        _write(self.repo, "other/b.py", "b = 1\n")
        _ = _commit(self.repo, "pipeline.toml", TWO_ITEMS)

    def run_gate(
        self, *extra: str, base: str | None = None, cwd: str | None = None
    ) -> tuple[int, str, RecordingRunner]:
        runner = RecordingRunner()
        code, out, err = self.tangier(
            "gate", "run", "backend", "--base", base or self.base, *extra, runner=runner, cwd=cwd
        )
        self.assertIn(code, (0, 2), err)
        return code, out + err, runner

    def record(self) -> dict[str, Any]:
        """The newest run in HEAD's record."""
        return _runs(self.repo, f"refs/tangier/gates/backend/{self.key(TWO_ITEMS)}")[-1]

    def rebase_onto_a_main_that_changes(self, rel: str, content: str) -> str:
        """Commit `rel` on a main that forks at `self.base`, rebase the branch onto it, and return main."""
        branch = _git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        _ = _git(self.repo, "checkout", "-q", "-b", "main-tip", self.base)
        main = _commit(self.repo, rel, content)
        _ = _git(self.repo, "checkout", "-q", branch)
        _ = _git(self.repo, "rebase", "-q", "main-tip")
        return main

    # SPEC: gate#comparator-falls-back-to-merge-base
    def test_with_no_record_the_gate_diffs_from_the_merge_base(self) -> None:
        _, _, runner = self.run_gate()
        self.assertEqual(runner.calls, BOTH)
        self.assertEqual(self.record()["base"], self.base)

    # SPEC: gate#comparator-newest-record
    def test_a_run_diffs_from_the_newest_record(self) -> None:
        _ = self.run_gate()
        recorded = _git(self.repo, "rev-parse", "HEAD")
        _ = _commit(self.repo, "other/b.py", "b = 2\n")
        _, _, runner = self.run_gate()
        self.assertEqual(runner.calls, OTHER_ONLY)
        self.assertEqual(self.record()["base"], recorded)

    # SPEC: gate#comparator-newest-record
    def test_a_record_at_head_runs_nothing(self) -> None:
        _ = self.run_gate()
        code, out, runner = self.run_gate()
        self.assertEqual((code, runner.calls), (0, []))
        self.assertIn("verified", out)

    # SPEC: gate#comparator-newest-record
    def test_a_dirty_tree_diffs_from_heads_record(self) -> None:
        _ = self.run_gate()
        _write(self.repo, "other/b.py", "b = 2\n")
        _, _, runner = self.run_gate()
        self.assertEqual(runner.calls, OTHER_ONLY)
        run = _runs(self.repo, f"refs/tangier/gates/backend/{self.worktree_key(TWO_ITEMS)}")[-1]
        self.assertEqual(run["base"], _git(self.repo, "rev-parse", "HEAD"))

    # SPEC: gate#comparator-newest-record
    def test_a_record_on_a_commit_that_is_not_an_ancestor_is_ignored(self) -> None:
        branch = _git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        _ = _git(self.repo, "checkout", "-q", "-b", "side")
        _ = _commit(self.repo, "other/b.py", "b = side\n")
        _ = self.run_gate()
        _ = _git(self.repo, "checkout", "-q", branch)
        _ = _commit(self.repo, "svc/a.py", "a = 3\n")
        _, _, runner = self.run_gate()
        self.assertEqual(runner.calls, BOTH)

    # SPEC: gate#comparator-first-parent
    def test_a_record_on_a_merged_branch_is_not_walked(self) -> None:
        # The side branch's record is reachable only through the merge's second parent.
        branch = _git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        _ = _git(self.repo, "checkout", "-q", "-b", "side")
        _ = _commit(self.repo, "other/b.py", "b = side\n")
        _ = self.run_gate()
        _ = _git(self.repo, "checkout", "-q", branch)
        # In the scope, so the merge's content differs from the side commit's.
        _ = _commit(self.repo, "svc/a.py", "a = 3\n")
        _ = _git(self.repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
        with mock.patch.object(git, "rev_list_first_parent", wraps=git.rev_list_first_parent) as walk:
            code, out, runner = self.run_gate()
        self.assertEqual(code, 0, out)
        walk.assert_called_once()
        self.assertEqual(runner.calls, BOTH)

    def merge_into_main(self, main_change: tuple[str, str] | None = None) -> None:
        """Check out a main that forks at `self.base`, and merge the branch into it as a PR's merge commit.

        HEAD's parents are then (main, the branch tip). With `main_change`, main first commits that file.
        """
        branch = _git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        _ = _git(self.repo, "checkout", "-q", "-b", "main-tip", self.base)
        if main_change:
            _ = _commit(self.repo, *main_change)
        _ = _git(self.repo, "merge", "-q", "--no-ff", "-m", "merge the PR", branch)

    # SPEC: gate#comparator-pr-head
    def test_a_pr_merge_commit_diffs_from_the_newest_record_on_the_pr_head(self) -> None:
        _ = self.run_gate()
        recorded = _git(self.repo, "rev-parse", "HEAD")
        _ = _commit(self.repo, "other/b.py", "b = 2\n")
        self.merge_into_main()
        _, out, runner = self.run_gate("--debug", base="HEAD^1")
        self.assertEqual(runner.calls, OTHER_ONLY)
        self.assertEqual(self.record()["base"], recorded)
        self.assertIn(f"  {recorded[:7]} (PR head) ", out)

    # SPEC: gate#comparator-pr-head
    def test_a_pr_merge_commit_after_main_moved_also_runs_mains_change(self) -> None:
        _ = self.run_gate()
        _ = _commit(self.repo, "other/b.py", "b = 2\n")
        self.merge_into_main(("svc/c.py", "c = 1\n"))
        _, _, runner = self.run_gate(base="HEAD^1")
        self.assertEqual(runner.calls, [["bin/test", "--dirs", "other,svc", "--files", "svc/c.py"], ["bin/lint"]])

    # SPEC: gate#comparator-pr-head
    # SPEC: gate#comparator-falls-back-to-merge-base
    def test_a_pr_merge_commit_with_no_record_on_the_branch_diffs_from_the_merge_base(self) -> None:
        self.merge_into_main()
        _, _, runner = self.run_gate(base="HEAD^1")
        self.assertEqual(runner.calls, BOTH)
        self.assertEqual(self.record()["base"], self.base)

    # SPEC: gate#comparator-first-parent
    # SPEC: gate#comparator-pr-head
    def test_the_pr_head_line_does_not_walk_a_branch_merged_into_it(self) -> None:
        branch = _git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        _ = _git(self.repo, "checkout", "-q", "-b", "side")
        _ = _commit(self.repo, "other/b.py", "b = side\n")
        _ = self.run_gate()
        _ = _git(self.repo, "checkout", "-q", branch)
        _ = _commit(self.repo, "svc/a.py", "a = 3\n")
        _ = _git(self.repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
        self.merge_into_main()
        _, _, runner = self.run_gate(base="HEAD^1")
        self.assertEqual(runner.calls, BOTH)
        self.assertEqual(self.record()["base"], self.base)

    # SPEC: gate#comparator-pr-head
    def test_a_shallow_clone_stops_the_pr_head_walk_at_its_boundary(self) -> None:
        # B has a record. The clone holds the merge commit and its parents only, so C but not B.
        _ = self.run_gate()
        recorded = _git(self.repo, "rev-parse", "HEAD")
        tip = _commit(self.repo, "other/b.py", "b = 2\n")
        self.merge_into_main()
        code, out, runner = self.run_gate("--debug", base="HEAD^1", cwd=self.shallow_clone(2))
        self.assertEqual(code, 0, out)
        self.assertNotIn("warning", out)
        self.assertEqual(runner.calls, BOTH)
        self.assertIn(f"  {tip[:7]} (PR head) ", out)
        self.assertNotIn(recorded[:7], out)

    # SPEC: gate#key-fails-closed
    def test_a_shallow_clone_with_no_merge_base_names_the_fix(self) -> None:
        self.merge_into_main()
        code, out, _ = self.run_gate(base="HEAD^1", cwd=self.shallow_clone(1))
        self.assertEqual(code, 2)
        self.assertIn("`fetch-depth` of 2 or more with `--base HEAD^1`", out)

    # SPEC: gate#comparator-newest-record
    def test_a_rebase_onto_a_main_that_leaves_the_scope_keeps_the_record(self) -> None:
        _ = self.run_gate()
        main = self.rebase_onto_a_main_that_changes("docs/notes.md", "main notes\n")
        rebased = _git(self.repo, "rev-parse", "HEAD")
        _ = _commit(self.repo, "other/b.py", "b = 2\n")
        _, _, runner = self.run_gate(base=main)
        self.assertEqual(runner.calls, OTHER_ONLY)
        self.assertEqual(self.record()["base"], rebased)

    # SPEC: gate#comparator-falls-back-to-merge-base
    def test_a_rebase_onto_a_main_that_changes_the_scope_falls_back_to_the_merge_base(self) -> None:
        _ = self.run_gate()
        main = self.rebase_onto_a_main_that_changes("bin/test", "#!/bin/sh\nexit 0\n")
        _, _, runner = self.run_gate(base=main)
        self.assertEqual(runner.calls, BOTH)
        self.assertEqual(self.record()["base"], main)

    # SPEC: gate#comparator-no-placeholder
    # SPEC: gate#key-no-placeholder-no-diff
    def test_a_gate_with_no_placeholder_checks_head_only(self) -> None:
        _ = _commit(self.repo, "pipeline.toml", TestNeed.NO_PLACEHOLDER)
        _ = self.run_gate()
        # A shallow CI checkout holds no `origin/main`.
        with mock.patch.object(git, "rev_list_first_parent", wraps=git.rev_list_first_parent) as walk:
            code, out, runner = self.run_gate(base="origin/main")
        self.assertEqual((code, runner.calls), (0, []))
        self.assertIn("verified", out)
        self.assertNotIn("warning", out)
        walk.assert_not_called()

    # SPEC: gate#comparator-newest-record
    def test_a_record_found_only_on_origin_is_a_comparator(self) -> None:
        origin = make_origin(self, self.repo)
        # The run publishes its record.
        _ = self.run_gate()
        other = self.clone_origin(origin)
        _ = _commit(other, "other/b.py", "b = 2\n")
        _, _, runner = self.run_gate(cwd=other)
        self.assertEqual(runner.calls, OTHER_ONLY)

    def run_in_ci(self, *extra: str, cwd: str | None = None) -> None:
        """Record a CI run at HEAD, as a GitHub Actions job on a push to main would."""
        with mock.patch.dict(os.environ, GITHUB_PUSH):
            self.assertEqual(self.run_gate("--full", *extra, cwd=cwd)[0], 0)

    # SPEC: gate#comparator-ignores-rejected
    def test_accept_ignores_a_local_run_and_walks_to_an_older_ci_run(self) -> None:
        self.run_in_ci()
        in_ci = _git(self.repo, "rev-parse", "HEAD")
        _ = _commit(self.repo, "other/b.py", "b = 2\n")
        _ = self.run_gate()
        # Without `--accept`, the local run at HEAD verifies the gate.
        self.assertIn("gate `backend`: verified", self.run_gate("--dry-run")[1])
        _, out, _ = self.run_gate("--accept", "ci", "--dry-run")
        self.assertIn(f"record at {in_ci[:7]}, local", out)
        self.assertIn("1 record(s) ignored by --accept", out)
        _, _, runner = self.run_gate("--accept", "ci")
        self.assertEqual(runner.calls, OTHER_ONLY)
        # The local pass joins the local run, and still does not satisfy `--accept ci`.
        self.assertEqual(self.record()["runner"]["kind"], "local")
        self.assertIn("gate `backend`: required", self.run_gate("--accept", "ci", "--dry-run")[1])

    # SPEC: gate#comparator-ignores-rejected
    def test_accept_with_only_local_runs_falls_back_to_the_merge_base(self) -> None:
        _ = self.run_gate()
        _ = _commit(self.repo, "other/b.py", "b = 2\n")
        _, _, runner = self.run_gate("--accept", "ci")
        self.assertEqual(runner.calls, BOTH)

    # SPEC: gate#comparator-ignores-rejected
    def test_accept_reads_the_runs_of_a_record_on_origin(self) -> None:
        origin = make_origin(self, self.repo)
        self.run_in_ci()
        in_ci = _git(self.repo, "rev-parse", "HEAD")
        _ = self.run_gate("--full")
        other = self.clone_origin(origin)
        _ = _commit(other, "other/b.py", "b = 2\n")
        _, out, _ = self.run_gate("--accept", "ci", "--dry-run", cwd=other)
        self.assertIn(f"record at {in_ci[:7]}, origin", out)
        _, _, runner = self.run_gate("--accept", "kind=ci,event=push", cwd=other)
        self.assertEqual(runner.calls, OTHER_ONLY)
        _, _, runner = self.run_gate("--accept", "kind=ci,event=pull_request", "--read-only", cwd=other)
        self.assertEqual(runner.calls, BOTH)

    # SPEC: gate#run-debug
    def test_debug_marks_each_run_accepted_or_ignored(self) -> None:
        with (
            mock.patch.object(gate, "now", return_value=datetime(2026, 3, 1, tzinfo=UTC)),
            _taking(12.34),
        ):
            self.run_in_ci()
        with (
            mock.patch.object(gate, "now", return_value=datetime(2026, 3, 2, tzinfo=UTC)),
            _taking(245.0),
        ):
            _ = self.run_gate("--full")
        code, out, _ = self.run_gate("--accept", "ci", "--dry-run", "--debug")
        self.assertEqual(code, 0)
        self.assertIn(
            "    ci github-actions push refs/heads/main job=backend 2026-03-01T00:00:00+00:00 12.3s (accepted)\n", out
        )
        self.assertIn(
            f"    local dev@example.com@{socket.gethostname()} 2026-03-02T00:00:00+00:00 4m05s (ignored)\n", out
        )

    # SPEC: gate#run-full
    def test_full_fills_every_list_from_the_whole_tree(self) -> None:
        # The diff from HEAD is empty, as on a push to main or a nightly, yet every list is complete.
        code, _, runner = self.run_gate("--full", base="HEAD")
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, FULL_TWO_ITEMS)

    # SPEC: gate#run-full
    def test_full_needs_no_merge_base(self) -> None:
        # A shallow checkout with no `origin/main` still fills every list, with no warning.
        code, out, runner = self.run_gate("--full", base="origin/main")
        self.assertEqual(code, 0)
        self.assertNotIn("merge base", out)
        self.assertEqual(runner.calls, FULL_TWO_ITEMS)


class TestRunMany(GateCase):
    SECOND = CONFIG + '[gate.lint]\ncmd = "bin/lint --all"\nscope = "svc"\n'

    def setUp(self) -> None:
        super().setUp()
        _ = _commit(self.repo, "pipeline.toml", self.SECOND)

    # SPEC: gate#run-all
    def test_all_runs_every_gate_in_config_order(self) -> None:
        runner = RecordingRunner()
        code, _, _ = self.tangier("gate", "run", "--all", "--base", self.base, runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [*SELECTED, ["bin/lint", "--all"]])
        self.assertEqual(len(_gate_refs(self.repo)), 2)

    # SPEC: gate#run-all
    def test_a_failing_gate_still_runs_the_rest_and_sets_the_exit_code(self) -> None:
        runner = RecordingRunner({("bin/test",): Result(3), ("bin/lint", "--all"): Result(4)})
        code, _, _ = self.tangier("gate", "run", "backend", "lint", "--base", self.base, runner=runner)
        self.assertEqual(code, 3)
        self.assertEqual(runner.calls, [SELECTED[0], ["bin/lint", "--all"]])
        self.assertEqual(_gate_refs(self.repo), [])

    # SPEC: gate#run-fail-fast
    def test_fail_fast_stops_at_the_first_failing_gate(self) -> None:
        runner = RecordingRunner({("bin/test",): Result(3)})
        code, _, err = self.tangier("gate", "run", "--all", "--fail-fast", "--base", self.base, runner=runner)
        self.assertEqual(code, 3)
        self.assertEqual(runner.calls, [SELECTED[0]])
        self.assertEqual(_gate_refs(self.repo), [])
        self.assertIn("--fail-fast: not run: lint\n", err)

    # SPEC: gate#run-fail-fast
    def test_fail_fast_runs_on_past_a_passing_gate(self) -> None:
        runner = RecordingRunner({("bin/lint", "--all"): Result(4)})
        code, _, err = self.tangier("gate", "run", "--all", "--fail-fast", "--base", self.base, runner=runner)
        self.assertEqual(code, 4)
        self.assertEqual(runner.calls, [*SELECTED, ["bin/lint", "--all"]])
        self.assertEqual(len(_gate_refs(self.repo)), 1)
        self.assertNotIn("not run", err)

    # SPEC: gate#run-fail-fast
    def test_fail_fast_stops_at_a_gate_error(self) -> None:
        # No `origin/main`, so the placeholder gate cannot resolve its base.
        runner = RecordingRunner()
        code, _, _ = self.tangier("gate", "run", "--all", "--fail-fast", "--base", "origin/main", runner=runner)
        self.assertEqual(code, 2)
        self.assertEqual(runner.calls, [])

    # SPEC: gate#run-all
    def test_no_gate_and_no_all_is_an_error(self) -> None:
        code, _, err = self.tangier("gate", "run", "--base", self.base)
        self.assertEqual(code, 2)
        self.assertIn("--all", err)

    # SPEC: gate#run-dry-run
    def test_dry_run_runs_nothing_and_writes_nothing(self) -> None:
        _write(self.repo, "scratch.txt", "dirty\n")
        runner = RecordingRunner()
        code, out, _ = self.tangier("gate", "run", "--all", "--dry-run", "--base", self.base, runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [])
        self.assertEqual(_gate_refs(self.repo), [])
        self.assertIn("gate `backend`: required", out)
        self.assertIn(f"base {self.base[:7]} (merge base with {self.base})", out)
        self.assertIn("bin/test --dirs svc --files svc/a.py", out)
        self.assertIn("gate `lint`: required", out)

    # SPEC: gate#run-dry-run
    def test_dry_run_reports_a_verified_gate(self) -> None:
        _ = self.tangier("gate", "run", "backend", "--base", self.base)
        _, out, _ = self.tangier("gate", "run", "--all", "--dry-run", "--base", self.base)
        self.assertIn("gate `backend`: verified", out)

    # SPEC: gate#run-debug
    def test_debug_prints_the_trail(self) -> None:
        recorded = _git(self.repo, "rev-parse", "HEAD")
        _ = self.tangier("gate", "run", "backend", "--base", self.base)
        _ = _commit(self.repo, "svc/a.py", "a = 3\n")
        head = _git(self.repo, "rev-parse", "HEAD")
        _, out, err = self.tangier("gate", "run", "backend", "--dry-run", "--debug", "--base", self.base)
        self.assertIn(f"{head[:7]} ", err)
        self.assertIn(f"{recorded[:7]} ", err)
        self.assertIn("miss", err)
        self.assertIn("local", err)
        self.assertIn("changed in scope: svc/a.py", err)
        self.assertIn(f"base {recorded[:7]} (record at {recorded[:7]}, local)", out)

    # SPEC: gate#run-debug
    def test_debug_labels_the_working_tree(self) -> None:
        _write(self.repo, "svc/a.py", "a = 3\n")
        _, _, err = self.tangier("gate", "run", "backend", "--dry-run", "--debug", "--base", self.base)
        self.assertIn(f"working tree ({self.tree()[:7]})", err)


class TestGroups(GateCase):
    GROUPED = (
        # `format` before `check`, so config order and name order differ.
        CONFIG + '[gate.lint]\nenv = { LINT = "1" }\n'
        '[gate.lint.format]\ncmd = "bin/lint format"\nscope = "inputs"\n'
        '[gate.lint.check]\ncmd = "bin/lint check"\nscope = "svc"\n'
    )
    CHECK = ["bin/lint", "check"]
    FORMAT = ["bin/lint", "format"]
    GATES = ("backend", "lint.format", "lint.check")

    def setUp(self) -> None:
        super().setUp()
        # Touches `inputs`, as `svc/a.py` touched `svc`, so every member is needed from `self.base`.
        _ = _commit(self.repo, "pipeline.toml", self.GROUPED)

    def run_gates(self, *selectors: str, runner: RecordingRunner | None = None) -> int:
        return self.tangier("gate", "run", *selectors, "--base", self.base, runner=runner)[0]

    # SPEC: gate#selector
    # SPEC: gate#group-ref
    def test_a_group_selector_runs_each_member_in_config_order_with_its_own_record(self) -> None:
        # The second overlaps and runs each gate once, in config order, not argument order.
        for selectors in (["lint"], ["lint.check", "lint"]):
            with self.subTest(selectors):
                for ref in _gate_refs(self.repo):
                    _ = _git(self.repo, "update-ref", "-d", ref)
                runner = RecordingRunner()
                self.assertEqual(self.run_gates(*selectors, runner=runner), 0)
                self.assertEqual(runner.calls, [self.FORMAT, self.CHECK])
                self.assertEqual((runner.envs[0] or {})["LINT"], "1")
                refs = _gate_refs(self.repo)
                self.assertEqual(
                    [ref.rsplit("/", 1)[0] for ref in refs],
                    [
                        "refs/tangier/gates/lint.check",
                        "refs/tangier/gates/lint.format",
                    ],
                )

    # SPEC: gate#selector
    def test_a_failing_member_still_runs_the_rest(self) -> None:
        runner = RecordingRunner({tuple(self.FORMAT): Result(3)})
        self.assertEqual(self.run_gates("lint", runner=runner), 3)
        self.assertEqual(runner.calls, [self.FORMAT, self.CHECK])
        self.assertEqual([ref.split("/")[3] for ref in _gate_refs(self.repo)], ["lint.check"])

    # SPEC: gate#group-records-per-member
    def test_a_change_voids_only_the_member_whose_scope_it_touches(self) -> None:
        self.assertEqual(self.run_gates("lint"), 0)
        _ = _commit(self.repo, "svc/a.py", "a = 3\n")
        runner = RecordingRunner()
        self.assertEqual(self.run_gates("lint", runner=runner), 0)
        self.assertEqual(runner.calls, [self.CHECK])

    # SPEC: gate#selector
    def test_an_unknown_selector_is_an_error(self) -> None:
        for selector in ("nope", "lint.nope", "lint.*"):
            with self.subTest(selector):
                code, _, err = self.tangier("gate", "run", selector, "--base", self.base)
                self.assertEqual(code, 2)
                self.assertIn(f"`{selector}`", err)

    # SPEC: gate#selector
    def test_key_prints_each_member_of_a_group(self) -> None:
        _, check, _ = self.tangier("gate", "key", "lint.check")
        _, both, _ = self.tangier("gate", "key", "lint")
        self.assertRegex(check, r"^[0-9a-f]{40}\n$")
        self.assertRegex(both.splitlines()[0], r"^lint\.format [0-9a-f]{40}$")
        self.assertEqual(both.splitlines()[1], f"lint.check {check.strip()}")

    # SPEC: gate#key-all
    def test_a_bare_key_prints_every_gate_in_config_order(self) -> None:
        expected = "".join(f"{name} {self.tangier('gate', 'key', name)[1]}" for name in self.GATES)
        self.assertEqual(self.tangier("gate", "key")[:2], (0, expected))

    # SPEC: gate#list
    def test_list_prints_each_gate_with_group_members_indented(self) -> None:
        self.assertEqual(
            self.tangier("gate", "list")[:2],
            (0, "backend\nlint (group)\n  lint.format\n  lint.check\n"),
        )

    # SPEC: gate#selector
    def test_a_group_is_verified_when_every_member_is(self) -> None:
        self.assertEqual(self.run_gates("lint.check"), 0)
        self.assertEqual(self.tangier("gate", "verified", "lint.check")[:2], (0, "verified\n"))
        self.assertEqual(self.tangier("gate", "verified", "lint")[:2], (1, "unverified\n"))
        self.assertEqual(self.run_gates("lint.format"), 0)
        self.assertEqual(self.tangier("gate", "verified", "lint")[:2], (0, "verified\n"))

    def group_outputs(self, base: str) -> dict[str, str]:
        _, out, _ = self.tangier("gate", "github-outputs", "--base", base)
        return dict(line.split("=", 1) for line in out.splitlines())

    # SPEC: gate#github-outputs
    # SPEC: gate#group-outputs
    def test_group_outputs_aggregate_their_members(self) -> None:
        outputs = self.group_outputs("HEAD")
        # Gates in name order, then the group, which has no `-key`.
        self.assertEqual(
            [name for name in outputs if name.startswith("lint")],
            [
                *(f"lint-check-{suffix}" for suffix in ("status", "run", "verified", "key")),
                *(f"lint-format-{suffix}" for suffix in ("status", "run", "verified", "key")),
                "lint-status",
                "lint-run",
                "lint-verified",
            ],
        )
        self.assertEqual(outputs["lint-check-status"], "not-needed")
        self.assertEqual(
            [outputs["lint-status"], outputs["lint-run"], outputs["lint-verified"]], ["not-needed", "false", "false"]
        )

        # One member verified and one not needed: the group is not verified.
        self.assertEqual(self.run_gates("lint.check"), 0)
        outputs = self.group_outputs("HEAD")
        self.assertEqual([outputs["lint-check-status"], outputs["lint-format-status"]], ["verified", "not-needed"])
        self.assertEqual(
            [outputs["lint-status"], outputs["lint-run"], outputs["lint-verified"]], ["not-needed", "false", "false"]
        )

        outputs = self.group_outputs(self.base)
        self.assertEqual([outputs["lint-check-status"], outputs["lint-format-status"]], ["verified", "required"])
        self.assertEqual(
            [outputs["lint-status"], outputs["lint-run"], outputs["lint-verified"]], ["required", "true", "false"]
        )

        self.assertEqual(self.run_gates("lint.format"), 0)
        outputs = self.group_outputs(self.base)
        self.assertEqual(
            [outputs["lint-status"], outputs["lint-run"], outputs["lint-verified"]], ["verified", "false", "true"]
        )

    # SPEC: gate#github-outputs-summary
    def test_summary_has_a_row_per_gate_group_and_member(self) -> None:
        self.assertEqual(self.run_gates("lint.check"), 0)
        head = _git(self.repo, "rev-parse", "--short=7", "HEAD")
        recorded = f"local · `{head}` · {socket.gethostname()}"
        for base, needed in (("HEAD", "not-needed"), (self.base, "required")):
            with self.subTest(base):
                code, out, err = self.tangier("gate", "github-outputs", "--base", base, "--summary")
                self.assertEqual(code, 0, err)
                # The table follows the output lines, which are unchanged.
                self.assertEqual(
                    out,
                    self.tangier("gate", "github-outputs", "--base", base)[1]
                    + _summary(
                        f"| `backend` | {needed} |  |",
                        f"| `lint` (group) | {needed} |  |",
                        f"| `lint.format` | {needed} |  |",
                        f"| `lint.check` | verified | {recorded} |",
                    ),
                )

    # SPEC: gate#github-outputs-summary
    def test_no_summary_flag_prints_no_table(self) -> None:
        code, out, err = self.tangier("gate", "github-outputs", "--base", self.base)
        self.assertEqual(code, 0, err)
        self.assertTrue(all(re.fullmatch(r"[a-z0-9-]+=\S*", line) for line in out.splitlines()), out)


class TestStore(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.origin = make_origin(self, self.repo)
        # `gate sync` prunes by age, so the March 2026 records these tests write must not age out.
        patcher = mock.patch.object(gate, "now", return_value=datetime(2026, 3, 10, tzinfo=UTC))
        _ = patcher.start()
        self.addCleanup(patcher.stop)

    def clone(self) -> str:
        return self.clone_origin(self.origin)

    def origin_refs(self) -> list[str]:
        return _git(self.origin, "for-each-ref", "--format=%(refname)", "refs/tangier").split()

    def record_and_push(self, when: datetime) -> str:
        """Record a pass at `when`, which publishes it, and return its key."""
        with mock.patch.object(gate, "now", return_value=when):
            code, _, err = self.tangier("gate", "run", "backend", "--base", self.base)
        self.assertEqual(code, 0, err)
        return self.key()

    @contextlib.contextmanager
    def offline(self) -> Iterator[None]:
        """`gate run` cannot publish, so its records stay local until a `gate sync`."""
        with mock.patch.object(gate, "publish", side_effect=gate.GateError("offline")):
            yield

    # SPEC: gate#verified-local-then-origin
    # SPEC: gate#run-publishes
    def test_a_pass_is_verified_from_a_second_clone_with_no_sync(self) -> None:
        other = self.clone()
        args = ("gate", "verified", "backend")
        self.assertEqual(self.tangier(*args, cwd=other)[:2], (1, "unverified\n"))

        code, out, err = self.tangier("gate", "run", "backend", "--base", self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("synced gate records with origin: pushed 1, pulled 0, merged 0, pruned 0\n", out)
        self.assertEqual(self.tangier(*args, cwd=other)[:2], (0, "verified\n"))

    # SPEC: gate#run-publishes
    def test_a_run_publishes_only_the_record_it_wrote(self) -> None:
        elsewhere = "refs/tangier/gates/backend/elsewhere"
        _put_blob(self.repo, elsewhere, _record("elsewhere", "2026-03-01T00:00:00+00:00"))
        _ = self.tangier("gate", "run", "backend", "--base", self.base)
        self.assertEqual(self.origin_refs(), [f"refs/tangier/gates/backend/{self.key()}"])
        self.assertIn(elsewhere, _gate_refs(self.repo))

    # SPEC: gate#run-publishes
    def test_a_publish_race_retries_on_the_written_gate_alone(self) -> None:
        other = self.clone()
        ref = f"refs/tangier/gates/backend/{self.key()}"
        with self.offline():
            self.run_on(other, 1)

        # `--full` reads no record, so the publish makes the first fetch.
        with self.race_after_fetch(lambda _: self.assertEqual(self.tangier("gate", "sync", cwd=other)[0], 0)) as fetch:
            self.run_on(self.repo, 2)
        # The middle fetch is the other clone's sync.
        self.assertEqual(
            [c.args for c in fetch.call_args_list],
            [("refs/tangier/gates/*",), ("refs/tangier/gates/*",), ("refs/tangier/gates/backend/*",)],
        )
        self.assertEqual(
            [run["time"] for run in _runs(self.origin, ref)], ["2026-03-01T00:00:00+00:00", "2026-03-02T00:00:00+00:00"]
        )

    # SPEC: gate#run-publishes
    def test_an_unreachable_origin_keeps_the_record_local_and_passes(self) -> None:
        _ = _git(self.repo, "remote", "set-url", "origin", os.path.join(self.origin, "gone"))
        code, _, err = self.tangier("gate", "run", "backend", "--base", self.base)
        self.assertEqual(code, 0)
        self.assertIn("warning: gate records stay local", err)
        self.assertIn("run `tangier gate sync` to retry", err)
        self.assertEqual(_gate_refs(self.repo), [f"refs/tangier/gates/backend/{self.key()}"])

    # SPEC: gate#run-publishes
    def test_only_a_run_that_writes_a_record_publishes(self) -> None:
        ref = f"refs/tangier/gates/backend/{self.key()}"
        with mock.patch.object(gate, "publish", return_value=gate.Synced(1, 0, 0, 0)) as publish:
            # The last `--base` wins, and an empty diff from HEAD makes the gate not-needed.
            cases = {"read-only": ("--read-only",), "dry-run": ("--dry-run",), "not-needed": ("--base", "HEAD")}
            for label, extra in cases.items():
                with self.subTest(label):
                    self.assertEqual(self.tangier("gate", "run", "backend", "--base", self.base, *extra)[0], 0)
            self.assertEqual(publish.call_count, 0)
            # The pass publishes; the next run is verified and does not.
            for _ in range(2):
                self.assertEqual(self.tangier("gate", "run", "backend", "--base", self.base)[0], 0)
        self.assertEqual([c.args for c in publish.call_args_list], [({ref},)])

    # SPEC: gate#run-publishes
    def test_a_publish_retry_pushes_a_ref_another_clone_deleted(self) -> None:
        ref = f"refs/tangier/gates/backend/{self.key()}"
        _ = self.record_and_push(datetime(2026, 3, 1, tzinfo=UTC))

        # The retry finds no ref on origin, so the lease is on the ref being absent.
        with self.race_after_fetch(lambda _: _git(self.repo, "push", "-q", "origin", f":{ref}")):
            self.run_on(self.repo, 2)
        self.assertEqual(
            [run["time"] for run in _runs(self.origin, ref)], ["2026-03-01T00:00:00+00:00", "2026-03-02T00:00:00+00:00"]
        )

    # SPEC: gate#run-publishes
    def test_a_publish_that_keeps_racing_warns_and_passes(self) -> None:
        ref = f"refs/tangier/gates/backend/{self.key()}"

        def another_clone_writes(race: int) -> None:
            _put_blob(self.repo, ref, _record(self.key(), f"2026-02-0{race}T00:00:00+00:00"), remote="origin")

        with self.race_after_fetch(another_clone_writes, times=gate.SYNC_ATTEMPTS):
            code, _, err = self.tangier("gate", "run", "backend", "--base", self.base, "--full")
        self.assertEqual(code, 0)
        self.assertIn("warning: gate records stay local (origin's gate records kept changing)", err)

    # SPEC: gate#run-publishes
    # SPEC: gate#run-all
    def test_a_failing_gate_still_publishes_an_earlier_pass(self) -> None:
        _ = _commit(self.repo, "pipeline.toml", TestGroups.GROUPED)
        runner = RecordingRunner({tuple(TestGroups.CHECK): Result(3)})
        code, _, _ = self.tangier("gate", "run", "lint", "--base", self.base, runner=runner)
        self.assertEqual(code, 3)
        self.assertEqual([ref.split("/")[3] for ref in self.origin_refs()], ["lint.format"])

    # SPEC: gate#run-reuses-record
    def test_run_reuses_a_record_found_on_origin(self) -> None:
        _ = self.record_and_push(datetime.now(UTC))
        runner = RecordingRunner()
        code, out, _ = self.tangier("gate", "run", "backend", "--base", self.base, runner=runner, cwd=self.clone())
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [])
        self.assertIn("origin", out)

    # SPEC: gate#sync
    def test_sync_with_no_local_record_succeeds(self) -> None:
        self.assertEqual(self.tangier("gate", "sync")[:2], (0, "gate records already in sync with origin\n"))
        self.assertEqual(self.origin_refs(), [])

    # SPEC: gate#github-outputs
    # SPEC: gate#status-values
    def test_github_outputs_names_each_gate(self) -> None:
        other = self.clone()
        args = ("gate", "github-outputs", "--base", self.base)
        key = self.key()
        self.assertEqual(
            self.tangier(*args, cwd=other)[1],
            f"backend-status=required\nbackend-run=true\nbackend-verified=false\nbackend-key={key}\n",
        )
        _ = self.record_and_push(datetime.now(UTC))
        self.assertEqual(
            self.tangier(*args, cwd=other)[1],
            f"backend-status=verified\nbackend-run=false\nbackend-verified=true\nbackend-key={key}\n",
        )

    # SPEC: gate#github-outputs
    # SPEC: gate#status-values
    def test_github_outputs_still_keys_a_gate_that_is_not_needed(self) -> None:
        # An empty diff needs no gate.
        key = self.key()
        _, out, _ = self.tangier("gate", "github-outputs", "--base", "HEAD")
        self.assertEqual(
            out, f"backend-status=not-needed\nbackend-run=false\nbackend-verified=false\nbackend-key={key}\n"
        )

    # SPEC: gate#run-full
    def test_github_outputs_full_marks_every_gate_and_group_required(self) -> None:
        # The same empty diff, and a record at HEAD: `--full` reads neither.
        second = CONFIG + '[gate.lint.style]\ncmd = "bin/lint"\nscope = "svc"\n'
        _ = _commit(self.repo, "pipeline.toml", second)
        self.assertEqual(self.tangier("gate", "run", "--all", "--base", "HEAD", "--full")[0], 0)
        self.assertIn("backend-status=verified\n", self.tangier("gate", "github-outputs", "--base", "HEAD")[1])
        _, out, _ = self.tangier("gate", "github-outputs", "--base", "HEAD", "--full")
        with contextlib.chdir(self.repo):
            cfg = parse_toml(second)
            keys = {name: gate.key(cfg, name, "HEAD") for name in ("backend", "lint.style")}
        self.assertEqual(
            out,
            f"backend-status=required\nbackend-run=true\nbackend-verified=false\nbackend-key={keys['backend']}\n"
            f"lint-style-status=required\nlint-style-run=true\nlint-style-verified=false\n"
            f"lint-style-key={keys['lint.style']}\n"
            "lint-status=required\nlint-run=true\nlint-verified=false\n",
        )

    # SPEC: gate#status-values
    def test_a_record_at_head_is_verified_before_the_need_test(self) -> None:
        # The same empty diff, but HEAD's content has a record.
        self.assertEqual(self.tangier("gate", "run", "backend", "--base", "HEAD", "--full")[0], 0)
        _, out, _ = self.tangier("gate", "github-outputs", "--base", "HEAD")
        self.assertIn("backend-status=verified\n", out)

    # SPEC: gate#github-outputs
    def test_github_outputs_reads_origin_once_for_all_gates(self) -> None:
        second = CONFIG + '[gate.lint]\ncmd = "bin/lint"\nscope = "svc"\n'
        _ = _commit(self.repo, "pipeline.toml", second)
        with mock.patch.object(git, "fetch", wraps=git.fetch) as fetch:
            _, out, _ = self.tangier("gate", "github-outputs", "--base", self.base)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual([line.split("=")[0] for line in out.splitlines()][::4], ["backend-status", "lint-status"])

    # SPEC: gate#github-outputs-summary
    def test_summary_names_a_ci_run_and_the_accept_filter_in_the_step_summary(self) -> None:
        with mock.patch.dict(os.environ, GITHUB_PUSH):
            _ = self.record_and_push(datetime(2026, 3, 1, tzinfo=UTC))
        head = _git(self.repo, "rev-parse", "--short=7", "HEAD")
        path = os.path.join(self.clone(), "summary.md")
        args = ("--summary", "--accept", "local", "--accept", "ci,event=push")
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": path}):
            code, out, err = self.tangier("gate", "github-outputs", "--base", self.base, *args, cwd=self.clone())
        self.assertEqual(code, 0, err)
        # Only the output lines reach stdout.
        self.assertEqual(out, self.tangier("gate", "github-outputs", "--base", self.base, cwd=self.clone())[1])
        with open(path) as fh:
            self.assertEqual(
                fh.read(),
                _summary(
                    f"| `backend` | verified | ci · `{head}` · [CI / backend](https://github.com/org/repo/actions/runs/123) |",
                    accept="`--accept local` or `--accept ci,event=push`",
                ),
            )

    # SPEC: gate#github-outputs-summary
    def test_summary_names_the_newest_run_that_accept_takes(self) -> None:
        with mock.patch.dict(os.environ, GITHUB_PUSH):
            _ = self.record_and_push(datetime(2026, 3, 1, tzinfo=UTC))
        # `--full` reads no record, so the newer local run joins the CI one.
        with mock.patch.object(gate, "now", return_value=datetime(2026, 3, 2, tzinfo=UTC)):
            self.assertEqual(self.tangier("gate", "run", "backend", "--full")[0], 0)
        head = _git(self.repo, "rev-parse", "--short=7", "HEAD")
        ci = f"ci · `{head}` · [CI / backend](https://github.com/org/repo/actions/runs/123)"
        local = f"local · `{head}` · {socket.gethostname()}"
        for accept, recorded in (((), local), (("--accept", "ci"), ci)):
            with self.subTest(accept):
                _, out, _ = self.tangier("gate", "github-outputs", "--base", self.base, "--summary", *accept)
                self.assertIn(f"| `backend` | verified | {recorded} |\n", out)

    # SPEC: gate#github-outputs-summary
    def test_summary_leaves_out_what_a_legacy_record_lacks(self) -> None:
        legacy = {"gate": "backend", "key": self.key(), "head": "abc", "time": "2026-03-01T00:00:00+00:00"}
        _put_blob(self.repo, f"refs/tangier/gates/backend/{self.key()}", json.dumps(legacy))
        _, out, _ = self.tangier("gate", "github-outputs", "--base", self.base, "--summary")
        self.assertIn("| `backend` | verified | local · `abc` |\n", out)

    # SPEC: gate#github-outputs-summary
    def test_summary_escapes_a_pipe_in_a_job_name(self) -> None:
        with mock.patch.dict(os.environ, {**GITHUB_PUSH, "GITHUB_JOB": "a|b [x]"}):
            _ = self.record_and_push(datetime(2026, 3, 1, tzinfo=UTC))
        _, out, _ = self.tangier("gate", "github-outputs", "--base", self.base, "--summary")
        self.assertIn(r"[CI / a\|b \[x\]](https://github.com/org/repo/actions/runs/123) |", out)

    # SPEC: gate#origin-unreachable
    def test_an_unreachable_origin_is_not_verified(self) -> None:
        _ = _git(self.repo, "remote", "set-url", "origin", os.path.join(self.origin, "gone"))
        code, out, err = self.tangier("gate", "verified", "backend")
        self.assertEqual((code, out), (1, "unverified\n"))
        self.assertIn("warning", err)

    def prune_after(self, days: int) -> None:
        """Commit `[gate] prune-after-days`, and push it so a later clone reads it too.

        The config is a key input, so record after this.
        """
        body = CONFIG.replace("[gate.backend]", f"[gate]\nprune-after-days = {days}\n\n[gate.backend]")
        _ = _commit(self.repo, "pipeline.toml", body)
        _ = _git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/main")

    # SPEC: gate#sync-prunes
    def test_sync_prunes_old_records_by_their_time(self) -> None:
        self.prune_after(30)
        old = self.record_and_push(datetime(2026, 1, 1, tzinfo=UTC))
        _ = _commit(self.repo, "svc/a.py", "a = 3\n")
        new = self.record_and_push(datetime(2026, 2, 20, tzinfo=UTC))
        # Not records: a blob that is not JSON, and a JSON object with no `time`.
        for name, content in (("junk", "not json"), ("timeless", "{}")):
            _put_blob(self.repo, f"refs/tangier/gates/backend/{name}", content, remote="origin")
        # Before the old record expires, another clone pulls it.
        other = self.clone()
        with mock.patch.object(gate, "now", return_value=datetime(2026, 1, 15, tzinfo=UTC)):
            self.assertEqual(self.tangier("gate", "sync", cwd=other)[0], 0)
        self.assertIn(f"refs/tangier/gates/backend/{old}", _gate_refs(other))

        with mock.patch.object(gate, "now", return_value=datetime(2026, 3, 1, tzinfo=UTC)):
            code, out, err = self.tangier("gate", "sync")

        self.assertEqual(code, 0)
        self.assertIn("pruned 1\n", out)
        kept = sorted(f"refs/tangier/gates/backend/{name}" for name in (new, "junk", "timeless"))
        self.assertEqual(self.origin_refs(), kept)
        self.assertEqual(_gate_refs(self.repo), kept)
        self.assertIn("skipped refs/tangier/gates/backend/junk", err)
        self.assertIn("skipped refs/tangier/gates/backend/timeless", err)

        # A clone that synced before the prune holds the old record, and does not put it back.
        with mock.patch.object(gate, "now", return_value=datetime(2026, 3, 1, tzinfo=UTC)):
            self.assertEqual(self.tangier("gate", "sync", cwd=other)[0], 0)
        self.assertNotIn(f"refs/tangier/gates/backend/{old}", self.origin_refs())
        self.assertEqual(_gate_refs(other), kept)

    # SPEC: gate#sync-prunes
    # SPEC: gate#prune-after-days
    def test_sync_keeps_a_record_whose_newest_run_is_recent(self) -> None:
        key = self.record_and_push(datetime(2026, 1, 1, tzinfo=UTC))
        with mock.patch.object(gate, "now", return_value=datetime(2026, 3, 1, tzinfo=UTC)):
            self.assertEqual(self.tangier("gate", "run", "backend", "--base", self.base, "--full")[0], 0)
        # The default limit is 90 days: past January's run, within March's.
        with mock.patch.object(gate, "now", return_value=datetime(2026, 5, 1, tzinfo=UTC)):
            self.assertEqual(self.tangier("gate", "sync")[1], "gate records already in sync with origin\n")
        self.assertEqual(self.origin_refs(), [f"refs/tangier/gates/backend/{key}"])
        self.assertEqual(len(_runs(self.origin, f"refs/tangier/gates/backend/{key}")), 2)

    # SPEC: gate#sync-drops-expired-local
    def test_sync_deletes_an_expired_local_record_and_does_not_push_it(self) -> None:
        ref = "refs/tangier/gates/backend/stale"
        _put_blob(self.repo, ref, _record("stale", "2026-01-01T00:00:00+00:00"))
        with mock.patch.object(gate, "now", return_value=datetime(2026, 5, 1, tzinfo=UTC)):
            code, out, _ = self.tangier("gate", "sync")
        self.assertEqual((code, out), (0, "synced gate records with origin: pushed 0, pulled 0, merged 0, pruned 1\n"))
        self.assertEqual(_gate_refs(self.repo), [])
        self.assertEqual(self.origin_refs(), [])

    # SPEC: gate#sync-prunes
    # SPEC: gate#sync-retries-on-race
    def test_sync_keeps_an_expired_record_that_gains_a_run_after_its_fetch(self) -> None:
        ref = "refs/tangier/gates/backend/revived"
        _put_blob(self.repo, ref, _record("revived", "2026-01-01T00:00:00+00:00"), remote="origin")
        fresh = _record("revived", "2026-01-01T00:00:00+00:00", "2026-04-30T00:00:00+00:00")

        # The delete is leased on the expired record, so it is rejected, and the retry pulls the fresh one.
        with (
            self.race_after_fetch(lambda _: _put_blob(self.repo, ref, fresh, remote="origin")),
            mock.patch.object(gate, "now", return_value=datetime(2026, 5, 1, tzinfo=UTC)),
        ):
            code, out, err = self.tangier("gate", "sync")
        self.assertEqual(
            (code, out, err), (0, "synced gate records with origin: pushed 0, pulled 1, merged 0, pruned 0\n", "")
        )
        self.assertEqual(_git(self.origin, "cat-file", "blob", ref), fresh)
        self.assertEqual(_git(self.repo, "rev-parse", ref), _git(self.origin, "rev-parse", ref))

    @contextlib.contextmanager
    def race_after_fetch(self, move_origin: Callable[[int], None], times: int = 1) -> Iterator[mock.MagicMock]:
        """After each of the next `times` fetches of origin, call `move_origin` with the race's number, from 1.

        The race lands between a sync's fetch and its push, so the push's lease is stale. Yields the patched fetch.
        """
        fetch = gate.fetch_origin
        races = 0

        def fetch_then_race(*args: str) -> None:
            nonlocal races
            fetch(*args)
            if races < times:
                races += 1
                move_origin(races)

        with mock.patch.object(gate, "fetch_origin", side_effect=fetch_then_race) as patched:
            yield patched

    def run_on(self, cwd: str, day: int) -> None:
        """Record a local pass in `cwd`, at 2026-03-`day`."""
        with mock.patch.object(gate, "now", return_value=datetime(2026, 3, day, tzinfo=UTC)), _taking(1.0):
            code, _, err = self.tangier("gate", "run", "backend", "--base", self.base, "--full", cwd=cwd)
        self.assertEqual(code, 0, err)

    # SPEC: gate#sync
    # SPEC: gate#sync-merges-runs
    # SPEC: gate#record-runs
    def test_sync_leaves_every_clone_and_origin_with_every_run_once(self) -> None:
        other = self.clone()
        ref = f"refs/tangier/gates/backend/{self.key()}"
        with (
            self.offline(),
            mock.patch.dict(os.environ, GITHUB_PUSH),
            mock.patch.object(gate, "now", return_value=datetime(2026, 3, 1, tzinfo=UTC)),
            _taking(12.34),
        ):
            code, _, err = self.tangier("gate", "run", "backend", "--base", self.base, cwd=other)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.tangier("gate", "sync", cwd=other)[0], 0)
        # Full: without `--accept`, origin's CI run already verifies the gate.
        with self.offline():
            self.run_on(self.repo, 2)
        self.assertEqual(
            self.tangier("gate", "sync")[1], "synced gate records with origin: pushed 0, pulled 0, merged 1, pruned 0\n"
        )
        self.assertEqual(
            self.tangier("gate", "sync", cwd=other)[1],
            "synced gate records with origin: pushed 0, pulled 1, merged 0, pruned 0\n",
        )

        on_origin = _git(self.origin, "rev-parse", ref)
        for cwd in (self.repo, other):
            self.assertEqual(self.tangier("gate", "sync", cwd=cwd)[1], "gate records already in sync with origin\n")
            self.assertEqual(_git(cwd, "rev-parse", ref), on_origin)
        self.assertEqual(_git(self.origin, "rev-parse", ref), on_origin)
        # The merge keeps each run whole.
        self.assertEqual(
            [(run["time"], run["runner"], run["duration"]) for run in _runs(self.origin, ref)],
            [("2026-03-01T00:00:00+00:00", GITHUB_PUSH_RAN_ON, 12.3), ("2026-03-02T00:00:00+00:00", LOCAL_RAN_ON, 1.0)],
        )

    # SPEC: gate#sync-merges-runs
    def test_sync_overwrites_an_unreadable_record_on_origin(self) -> None:
        ref = f"refs/tangier/gates/backend/{self.key()}"
        _put_blob(self.repo, ref, "not json", remote="origin")
        with self.offline():
            _ = self.tangier("gate", "run", "backend", "--base", self.base)
        code, out, err = self.tangier("gate", "sync")
        self.assertEqual((code, out), (0, "synced gate records with origin: pushed 1, pulled 0, merged 0, pruned 0\n"))
        self.assertIn(f"origin's {ref} is unreadable, so the local one replaces it", err)
        self.assertEqual([run["runner"] for run in _runs(self.origin, ref)], [LOCAL_RAN_ON])

    # SPEC: gate#sync-merges-runs
    def test_sync_keeps_origins_record_over_an_unreadable_local_one(self) -> None:
        ref = f"refs/tangier/gates/backend/{self.key()}"
        _ = self.record_and_push(datetime(2026, 3, 1, tzinfo=UTC))
        _put_blob(self.repo, ref, "not json")
        code, out, err = self.tangier("gate", "sync")
        self.assertEqual((code, out), (0, "synced gate records with origin: pushed 0, pulled 1, merged 0, pruned 0\n"))
        self.assertIn(f"kept origin's {ref}", err)
        self.assertEqual(_git(self.repo, "rev-parse", ref), _git(self.origin, "rev-parse", ref))
        self.assertEqual([run["runner"] for run in _runs(self.origin, ref)], [LOCAL_RAN_ON])

    # SPEC: gate#sync-pulls
    def test_sync_pulls_a_record_only_origin_holds(self) -> None:
        _ = self.record_and_push(datetime(2026, 3, 1, tzinfo=UTC))
        other = self.clone()
        ref = f"refs/tangier/gates/backend/{self.key()}"
        self.assertEqual(_gate_refs(other), [])
        self.assertEqual(
            self.tangier("gate", "sync", cwd=other)[:2],
            (0, "synced gate records with origin: pushed 0, pulled 1, merged 0, pruned 0\n"),
        )
        self.assertEqual(_git(other, "rev-parse", ref), _git(self.origin, "rev-parse", ref))

    # SPEC: gate#sync-retries-on-race
    # SPEC: gate#sync-pulls
    def test_sync_retries_when_another_clone_pushes_after_its_fetch(self) -> None:
        other, third = self.clone(), self.clone()
        ref = f"refs/tangier/gates/backend/{self.key()}"
        with self.offline():
            for cwd, day in ((other, 1), (self.repo, 2), (third, 3)):
                self.run_on(cwd, day)
        self.assertEqual(self.tangier("gate", "sync", cwd=other)[0], 0)
        # Pulled in the first attempt, it counts once.
        elsewhere = "refs/tangier/gates/backend/elsewhere"
        _put_blob(other, elsewhere, _record("elsewhere", "2026-03-01T00:00:00+00:00"), remote="origin")

        with self.race_after_fetch(lambda _: self.assertEqual(self.tangier("gate", "sync", cwd=third)[0], 0)):
            code, out, err = self.tangier("gate", "sync")
        self.assertEqual(
            (code, out, err), (0, "synced gate records with origin: pushed 0, pulled 1, merged 1, pruned 0\n", "")
        )
        self.assertEqual(
            [run["time"] for run in _runs(self.origin, ref)],
            ["2026-03-01T00:00:00+00:00", "2026-03-02T00:00:00+00:00", "2026-03-03T00:00:00+00:00"],
        )

    # SPEC: gate#sync-retries-on-race
    def test_sync_retries_when_another_clone_creates_the_ref_after_its_fetch(self) -> None:
        other = self.clone()
        ref = f"refs/tangier/gates/backend/{self.key()}"
        with self.offline():
            for cwd, day in ((other, 1), (self.repo, 2)):
                self.run_on(cwd, day)

        # Origin lacked the ref at the fetch, so the lease says it must not exist.
        with self.race_after_fetch(lambda _: self.assertEqual(self.tangier("gate", "sync", cwd=other)[0], 0)):
            code, out, err = self.tangier("gate", "sync")
        self.assertEqual(
            (code, out, err), (0, "synced gate records with origin: pushed 0, pulled 0, merged 1, pruned 0\n", "")
        )
        self.assertEqual(
            [run["time"] for run in _runs(self.origin, ref)], ["2026-03-01T00:00:00+00:00", "2026-03-02T00:00:00+00:00"]
        )

    # SPEC: gate#sync-retries-on-race
    def test_sync_gives_up_when_origin_keeps_changing(self) -> None:
        ref = f"refs/tangier/gates/backend/{self.key()}"
        _ = self.record_and_push(datetime(2026, 3, 1, tzinfo=UTC))
        with self.offline():
            self.run_on(self.repo, 2)
        written: list[str] = []

        def another_clone_writes(race: int) -> None:
            written.append(_record(self.key(), f"2026-02-0{race}T00:00:00+00:00"))
            _put_blob(self.repo, ref, written[-1], remote="origin")

        with self.race_after_fetch(another_clone_writes, times=gate.SYNC_ATTEMPTS):
            code, _, err = self.tangier("gate", "sync")
        self.assertEqual(code, 2)
        self.assertIn("origin's gate records kept changing; run `gate sync` again", err)
        self.assertEqual(len(written), gate.SYNC_ATTEMPTS)
        self.assertEqual(_git(self.origin, "cat-file", "blob", ref), written[-1])

    # SPEC: gate#sync-retries-on-race
    def test_a_push_origin_refuses_is_not_retried(self) -> None:
        hook = os.path.join(self.origin, "hooks", "pre-receive")
        with open(hook, "w") as f:
            _ = f.write("#!/bin/sh\necho no gate refs here >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        with self.offline():
            self.run_on(self.repo, 1)
        code, _, err = self.tangier("gate", "sync")
        self.assertEqual(code, 2)
        self.assertIn("no gate refs here", err)
        self.assertNotIn("kept changing", err)

    # SPEC: gate#comparator-ignores-rejected
    def test_an_unreadable_record_on_origin_is_a_miss(self) -> None:
        _put_blob(self.repo, f"refs/tangier/gates/backend/{self.key()}", "not json", remote="origin")
        code, out, err = self.tangier("gate", "verified", "backend", cwd=self.clone())
        self.assertEqual((code, out), (1, "unverified\n"))
        self.assertIn("so it counts as absent", err)

    # SPEC: gate#github-outputs
    # SPEC: gate#accept-filter
    def test_github_outputs_counts_only_accepted_runs(self) -> None:
        _ = self.record_and_push(datetime.now(UTC))
        args = ("gate", "github-outputs", "--base", self.base)
        other = self.clone()
        self.assertIn("backend-status=verified\n", self.tangier(*args, cwd=other)[1])
        self.assertIn("backend-status=required\n", self.tangier(*args, "--accept", "ci", cwd=other)[1])


class TestCustomPackageIsNotABucket(GateCase):
    # SPEC: gate#custom-package-not-a-bucket
    def test_absent_from_sha_all(self) -> None:
        _, out, _ = self.tangier("changemap", "sha", "--all")
        self.assertEqual([line.split("=")[0] for line in out.splitlines()], ["SVC_VERSION"])

    # SPEC: gate#custom-package-not-a-bucket
    def test_absent_from_the_build_matrix(self) -> None:
        first = _git(self.repo, "rev-parse", "HEAD")
        # Both packages change; only the bucket is a build package.
        _ = _commit(self.repo, "bin/test", "#!/bin/sh\nexit 0\n")
        _ = _commit(self.repo, "svc/a.py", "a = 3\n")
        _, out, _ = self.tangier("changemap", "build-matrix", "--base", first)
        self.assertEqual(out, 'build-packages=["svc"]\nbuild-packages-empty=false\n')
