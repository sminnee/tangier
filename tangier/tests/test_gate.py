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
import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from typing import Any
from unittest import mock

import tangier
from tangier import cli, gate, git
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
    "bin/test": "#!/bin/sh\n",
    "docs/notes.md": "notes\n",
    ".gitignore": "*.log\n",
}

# The gate's commands when the diff selects nothing: each list is an empty argument.
NOTHING_SELECTED = [["bin/test", "--dirs", "", "--files", ""], ["bin/lint"]]


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


class GateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = make_git_repo(self, FILES)
        _ = _git(self.repo, "config", "user.email", "dev@example.com")
        # A runner's `$GITHUB_OUTPUT` must not receive the test's output lines.
        patcher = mock.patch.dict(os.environ)
        _ = patcher.start()
        self.addCleanup(patcher.stop)
        _ = os.environ.pop("GITHUB_OUTPUT", None)

    def tangier(self, *argv: str, runner: Any = None, cwd: str | None = None) -> tuple[int, str, str]:
        """Run the CLI in a repo. Returns (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.chdir(cwd or self.repo), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), runner=runner or RecordingRunner())
        return code, out.getvalue(), err.getvalue()

    def key(self, body: str = CONFIG, *, base: str = "HEAD", head: str = "HEAD") -> str:
        with contextlib.chdir(self.repo):
            return gate.key(parse_toml(body), "backend", base, head)


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
    def test_a_different_item_list_moves_the_key(self) -> None:
        first = _git(self.repo, "rev-parse", "HEAD")
        _ = _commit(self.repo, "svc/a.py", "a = 2\n")
        # Same head, so the same tree. Against `first` the diff selects `svc`;
        # against HEAD it selects nothing.
        self.assertNotEqual(self.key(base=first), self.key(base="HEAD"))

    # SPEC: gate#key-env
    def test_a_different_env_moves_the_key(self) -> None:
        self.assertNotEqual(self.key(CONFIG.replace('TEST_DB = "1"', 'TEST_DB = "2"')), self.key())

    # SPEC: gate#key-fails-closed
    def test_an_unresolvable_head_raises(self) -> None:
        with self.assertRaises(gate.GateError) as ctx:
            _ = self.key(head="no-such-ref")
        self.assertIn("no-such-ref", str(ctx.exception))

    # SPEC: gate#key-fails-closed
    def test_an_unresolvable_base_raises(self) -> None:
        # The lenient diff would read this as "nothing changed" and give the
        # same key as a run that selected nothing.
        with self.assertRaises(gate.GateError) as ctx:
            _ = self.key(base="origin/main")
        self.assertIn("origin/main", str(ctx.exception))

    # SPEC: gate#key-no-placeholder-no-diff
    def test_a_gate_with_no_placeholder_needs_no_base(self) -> None:
        # A shallow CI checkout holds no `origin/main`.
        body = CONFIG.replace("bin/test --dirs {unittest-items} --files {svc-files}", "bin/test")
        self.assertEqual(self.key(body, base="origin/main"), self.key(body, base="HEAD"))

    # SPEC: gate#key-fails-closed
    def test_a_scope_entry_that_matches_no_tracked_file_raises(self) -> None:
        # `svc` still matches, so the scope as a whole is not empty.
        body = CONFIG.replace('"inputs"]', '"ghost"]') + '[ghost]\npaths = "ghost/**"\n'
        with self.assertRaises(gate.GateError) as ctx:
            _ = self.key(body)
        self.assertIn("`ghost` matches no tracked file", str(ctx.exception))


class DirtyingRunner(RecordingRunner):
    """A runner whose commands leave an untracked file behind."""

    def __init__(self, root: str) -> None:
        super().__init__()
        self.root = root

    def run(self, argv: list[str], **kwargs: Any) -> Result:
        _write(self.root, "coverage.out", "x")
        return super().run(argv, **kwargs)


class CommittingRunner(RecordingRunner):
    """A runner whose commands commit, so the tree stays clean and HEAD moves."""

    def __init__(self, root: str) -> None:
        super().__init__()
        self.root = root

    def run(self, argv: list[str], **kwargs: Any) -> Result:
        _ = _git(self.root, "commit", "-q", "--allow-empty", "-m", "from the gate")
        return super().run(argv, **kwargs)


class TestRun(GateCase):
    def run_gate(self, *extra: str, runner: Any = None) -> tuple[int, str, str]:
        return self.tangier("gate", "run", "backend", "--base", "HEAD", *extra, runner=runner)

    # SPEC: gate#run-refuses-dirty-tree
    def assert_refused(self) -> None:
        runner = RecordingRunner()
        code, _, err = self.run_gate(runner=runner)
        self.assertEqual(code, 2)
        self.assertIn("dirty", err)
        self.assertEqual(runner.calls, [])
        self.assertEqual(_gate_refs(self.repo), [])

    # SPEC: gate#run-refuses-dirty-tree
    def test_a_tracked_change_is_refused(self) -> None:
        _write(self.repo, "svc/a.py", "dirty\n")
        self.assert_refused()

    # SPEC: gate#run-refuses-dirty-tree
    def test_an_untracked_file_is_refused(self) -> None:
        _write(self.repo, "scratch.txt", "dirty\n")
        self.assert_refused()

    # SPEC: gate#run-refuses-dirty-tree
    def test_an_untracked_file_is_refused_when_git_status_hides_untracked_files(self) -> None:
        _ = _git(self.repo, "config", "status.showUntrackedFiles", "no")
        _write(self.repo, "scratch.txt", "dirty\n")
        self.assert_refused()

    # SPEC: gate#run-refuses-dirty-tree
    def test_an_ignored_file_does_not_make_the_tree_dirty(self) -> None:
        _write(self.repo, "debug.log", "ignored\n")
        self.assertEqual(self.run_gate()[0], 0)
        self.assertEqual(len(_gate_refs(self.repo)), 1)

    # SPEC: gate#run-stops-at-first-failure
    def test_a_failing_command_stops_the_run_and_writes_no_record(self) -> None:
        runner = RecordingRunner({("bin/test",): Result(3)})
        code, _, _ = self.run_gate(runner=runner)
        self.assertEqual(code, 3)
        self.assertEqual(runner.calls, NOTHING_SELECTED[:1])
        self.assertEqual(_gate_refs(self.repo), [])

    # SPEC: gate#run-dirty-after
    def test_a_run_that_dirties_the_tree_writes_no_record(self) -> None:
        code, _, err = self.run_gate(runner=DirtyingRunner(self.repo))
        self.assertEqual(code, 1)
        self.assertIn("no record was written", err)
        self.assertEqual(_gate_refs(self.repo), [])

    # SPEC: gate#run-dirty-after
    def test_a_run_that_moves_head_writes_no_record(self) -> None:
        code, _, err = self.run_gate(runner=CommittingRunner(self.repo))
        self.assertEqual(code, 1)
        self.assertIn("moved HEAD", err)
        self.assertEqual(_gate_refs(self.repo), [])

    # SPEC: gate#placeholder-whole-token
    def test_placeholders_become_the_selected_lists(self) -> None:
        first = _git(self.repo, "rev-parse", "HEAD")
        _ = _commit(self.repo, "svc/a.py", "a = 2\n")
        runner = RecordingRunner()
        code, _, _ = self.tangier("gate", "run", "backend", "--base", first, runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [["bin/test", "--dirs", "svc", "--files", "svc/a.py"], ["bin/lint"]])

    # SPEC: gate#run-records-pass
    # SPEC: gate#record-contents
    def test_a_pass_on_a_clean_tree_writes_the_record(self) -> None:
        runner = RecordingRunner()
        with mock.patch.object(gate, "now", return_value=datetime(2026, 3, 1, 12, 0, tzinfo=UTC)):
            code, _, _ = self.run_gate(runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, NOTHING_SELECTED)
        self.assertEqual([env["TEST_DB"] for env in runner.envs], ["1", "1"])
        # The gate's env is added to the caller's, not a replacement for it.
        self.assertIn("PATH", runner.envs[0])

        key = self.key()
        self.assertEqual(_gate_refs(self.repo), [f"refs/tangier/gates/backend/{key}"])
        record = json.loads(_git(self.repo, "cat-file", "blob", f"refs/tangier/gates/backend/{key}"))
        self.assertEqual(
            record,
            {
                "gate": "backend",
                "key": key,
                "head": _git(self.repo, "rev-parse", "HEAD"),
                "user": "dev@example.com",
                "time": "2026-03-01T12:00:00+00:00",
                "tangier": tangier.__version__,
                "commands": NOTHING_SELECTED,
            },
        )

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

    # SPEC: gate#run-no-record
    def test_no_record_runs_on_a_dirty_tree_and_writes_nothing(self) -> None:
        _write(self.repo, "scratch.txt", "dirty\n")
        runner = RecordingRunner()
        code, _, _ = self.run_gate("--no-record", runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, NOTHING_SELECTED)
        self.assertEqual(_gate_refs(self.repo), [])

    # SPEC: gate#run-no-record
    def test_no_record_ignores_an_existing_record(self) -> None:
        _ = self.run_gate()
        runner = RecordingRunner()
        _ = self.run_gate("--no-record", runner=runner)
        self.assertEqual(runner.calls, NOTHING_SELECTED)

    # SPEC: gate#unknown-gate
    def test_an_unknown_gate_is_an_error(self) -> None:
        code, _, err = self.tangier("gate", "key", "nope")
        self.assertEqual(code, 2)
        self.assertIn("backend", err)


class TestStore(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.origin = make_origin(self, self.repo)

    def clone(self) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _ = subprocess.run(["git", "clone", "-q", self.origin, tmp.name], capture_output=True, check=True)
        return tmp.name

    def origin_refs(self) -> list[str]:
        return _git(self.origin, "for-each-ref", "--format=%(refname)", "refs/tangier").split()

    def record_and_push(self, when: datetime) -> str:
        """Record a pass at `when`, push it, and return its key."""
        with mock.patch.object(gate, "now", return_value=when):
            code, _, err = self.tangier("gate", "run", "backend", "--base", "HEAD")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.tangier("gate", "push")[0], 0)
        return self.key()

    # SPEC: gate#verified-local-then-origin
    # SPEC: gate#push
    def test_a_pushed_record_is_verified_from_a_second_clone(self) -> None:
        other = self.clone()
        args = ("gate", "verified", "backend", "--base", "HEAD")
        self.assertEqual(self.tangier(*args, cwd=other)[:2], (1, "unverified\n"))

        _ = self.tangier("gate", "run", "backend", "--base", "HEAD")
        # Local only: the record has not left the first clone.
        self.assertEqual(self.tangier(*args)[:2], (0, "verified\n"))
        self.assertEqual(self.tangier(*args, cwd=other)[:2], (1, "unverified\n"))

        self.assertEqual(self.tangier("gate", "push")[0], 0)
        self.assertEqual(self.tangier(*args, cwd=other)[:2], (0, "verified\n"))

    # SPEC: gate#run-reuses-record
    def test_run_reuses_a_record_found_on_origin(self) -> None:
        _ = self.record_and_push(datetime.now(UTC))
        runner = RecordingRunner()
        code, out, _ = self.tangier("gate", "run", "backend", "--base", "HEAD", runner=runner, cwd=self.clone())
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [])
        self.assertIn("origin", out)

    # SPEC: gate#push
    def test_push_with_no_local_record_succeeds(self) -> None:
        self.assertEqual(self.tangier("gate", "push")[0], 0)
        self.assertEqual(self.origin_refs(), [])

    # SPEC: gate#github-outputs
    def test_github_outputs_names_each_gate(self) -> None:
        other = self.clone()
        args = ("gate", "github-outputs", "--base", "HEAD")
        key = self.key()
        self.assertEqual(self.tangier(*args, cwd=other)[1], f"backend-verified=false\nbackend-key={key}\n")
        _ = self.record_and_push(datetime.now(UTC))
        self.assertEqual(self.tangier(*args, cwd=other)[1], f"backend-verified=true\nbackend-key={key}\n")

    # SPEC: gate#github-outputs
    def test_github_outputs_reads_origin_once_for_all_gates(self) -> None:
        second = CONFIG + '[gate.lint]\ncmd = "bin/lint"\nscope = "svc"\n'
        _ = _commit(self.repo, "pipeline.toml", second)
        with mock.patch.object(git, "ls_remote", wraps=git.ls_remote) as ls_remote:
            _, out, _ = self.tangier("gate", "github-outputs", "--base", "HEAD")
        self.assertEqual(ls_remote.call_count, 1)
        self.assertEqual([line.split("=")[0] for line in out.splitlines()][::2], ["backend-verified", "lint-verified"])

    # SPEC: gate#origin-unreachable
    def test_an_unreachable_origin_is_not_verified(self) -> None:
        _ = _git(self.repo, "remote", "set-url", "origin", os.path.join(self.origin, "gone"))
        code, out, err = self.tangier("gate", "verified", "backend", "--base", "HEAD")
        self.assertEqual((code, out), (1, "unverified\n"))
        self.assertIn("warning", err)

    # SPEC: gate#prune-by-record-time
    # SPEC: gate#prune-skips-unreadable
    def test_prune_deletes_old_records_on_origin_by_their_time(self) -> None:
        old = self.record_and_push(datetime(2026, 1, 1, tzinfo=UTC))
        _ = _commit(self.repo, "svc/a.py", "a = 2\n")
        new = self.record_and_push(datetime(2026, 2, 20, tzinfo=UTC))
        # Not records: a blob that is not JSON, and a JSON object with no `time`.
        for name, content in (("junk", "not json"), ("timeless", "{}")):
            blob = subprocess.run(
                ["git", "-C", self.repo, "hash-object", "-w", "--stdin"],
                input=content,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            _ = _git(self.repo, "push", "-q", "origin", f"{blob}:refs/tangier/gates/backend/{name}")

        with mock.patch.object(gate, "now", return_value=datetime(2026, 3, 1, tzinfo=UTC)):
            code, out, err = self.tangier("gate", "prune", "--older-than", "30")

        self.assertEqual(code, 0)
        self.assertEqual(
            self.origin_refs(),
            sorted(f"refs/tangier/gates/backend/{name}" for name in (new, "junk", "timeless")),
        )
        self.assertIn(f"refs/tangier/gates/backend/{old}", out)
        self.assertIn("refs/tangier/gates/backend/junk", err)
        self.assertIn("refs/tangier/gates/backend/timeless", err)

        # The old local record goes too. Left in place, the next push would
        # put the pruned ref back on origin.
        self.assertEqual(_gate_refs(self.repo), [f"refs/tangier/gates/backend/{new}"])
        self.assertEqual(self.tangier("gate", "push")[0], 0)
        self.assertNotIn(f"refs/tangier/gates/backend/{old}", self.origin_refs())

    # SPEC: gate#prune-by-record-time
    def test_prune_rejects_a_limit_below_one_day(self) -> None:
        # A negative limit would put the cutoff in the future and delete every record.
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            _ = self.tangier("gate", "prune", "--older-than", "-1")


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
        _ = _commit(self.repo, "svc/a.py", "a = 2\n")
        _, out, _ = self.tangier("changemap", "build-matrix", "--base", first)
        self.assertEqual(out, 'build-packages=["svc"]\nbuild-packages-empty=false\n')
