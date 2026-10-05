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
# The gate's commands for `GateCase`'s diff, which changes `svc/a.py`.
SELECTED = [["bin/test", "--dirs", "svc", "--files", "svc/a.py"], ["bin/lint"]]


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
        # The diff from `self.base` to HEAD touches the scope and selects `svc`, so the gate is needed.
        self.base = _git(self.repo, "rev-parse", "HEAD")
        _ = _commit(self.repo, "svc/a.py", "a = 2\n")
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

    def key(self, body: str = CONFIG, *, head: str = "HEAD") -> str:
        with contextlib.chdir(self.repo):
            return gate.key(parse_toml(body), "backend", head)


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
            plans = [gate.plan(cfg, "backend", base, records=False) for base in (first, "HEAD")]
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
        self.assertIn("origin/main", err)
        self.assertEqual(_gate_refs(self.repo), [])

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
        return self.tangier("gate", "run", "backend", "--base", self.base, *extra, runner=runner)

    # SPEC: gate#run-refuses-dirty-tree
    def assert_refused(self, *extra: str) -> None:
        runner = RecordingRunner()
        code, _, err = self.run_gate(*extra, runner=runner)
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
        self.assertEqual(runner.calls, SELECTED[:1])
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
        runner = RecordingRunner()
        code, _, _ = self.run_gate(runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [["bin/test", "--dirs", "svc", "--files", "svc/a.py"], ["bin/lint"]])

    # SPEC: gate#run-records-pass
    # SPEC: gate#record-contents
    def test_a_pass_on_a_clean_tree_writes_the_record(self) -> None:
        runner = RecordingRunner()
        with mock.patch.object(gate, "now", return_value=datetime(2026, 3, 1, 12, 0, tzinfo=UTC)):
            code, _, _ = self.run_gate(runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, SELECTED)
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
                "base": self.base,
                "user": "dev@example.com",
                "time": "2026-03-01T12:00:00+00:00",
                "tangier": tangier.__version__,
                "commands": SELECTED,
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

    # SPEC: gate#run-read-only
    def test_read_only_reuses_a_record_on_a_clean_tree(self) -> None:
        _ = self.run_gate()
        runner = RecordingRunner()
        code, out, _ = self.run_gate("--read-only", runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [])
        self.assertIn("verified", out)

    # SPEC: gate#run-read-only
    def test_read_only_on_a_dirty_tree_runs_and_does_not_read_a_record(self) -> None:
        _ = self.run_gate()
        _write(self.repo, "scratch.txt", "dirty\n")
        runner = RecordingRunner()
        code, _, err = self.run_gate("--read-only", runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, SELECTED)
        self.assertIn("dirty", err)

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

    # SPEC: gate#run-force
    def test_force_runs_a_verified_gate_and_writes_the_record(self) -> None:
        ref = f"refs/tangier/gates/backend/{self.key()}"
        with mock.patch.object(gate, "now", return_value=datetime(2026, 3, 1, tzinfo=UTC)):
            _ = self.run_gate()
        first = _git(self.repo, "rev-parse", ref)
        runner = RecordingRunner()
        with mock.patch.object(gate, "now", return_value=datetime(2026, 3, 2, tzinfo=UTC)):
            code, _, _ = self.run_gate("--force", runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, SELECTED)
        # A new record, with the later time, replaces the first.
        self.assertNotEqual(_git(self.repo, "rev-parse", ref), first)

    # SPEC: gate#run-refuses-dirty-tree
    # SPEC: gate#run-force
    def test_force_refuses_a_dirty_tree(self) -> None:
        _write(self.repo, "scratch.txt", "dirty\n")
        self.assert_refused("--force")

    # SPEC: gate#run-read-only
    # SPEC: gate#run-force
    def test_read_only_with_force_runs_on_a_dirty_tree_and_writes_nothing(self) -> None:
        _write(self.repo, "scratch.txt", "dirty\n")
        runner = RecordingRunner()
        code, _, _ = self.run_gate("--read-only", "--force", runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, SELECTED)
        self.assertEqual(_gate_refs(self.repo), [])

    # SPEC: gate#run-read-only
    # SPEC: gate#run-force
    def test_read_only_with_force_ignores_an_existing_record(self) -> None:
        _ = self.run_gate()
        runner = RecordingRunner()
        _ = self.run_gate("--read-only", "--force", runner=runner)
        self.assertEqual(runner.calls, SELECTED)

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

    # SPEC: gate#run-force
    def test_force_runs_a_gate_that_is_not_needed(self) -> None:
        self.use_config(self.NO_PLACEHOLDER)
        _ = _commit(self.repo, "docs/notes.md", "more notes\n")
        code, _, _, runner = self.run_gate("--force")
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [["bin/test"], ["bin/lint"]])
        self.assertEqual(len(_gate_refs(self.repo)), 1)

    # SPEC: gate#run-refuses-dirty-tree
    def test_a_dirty_tree_is_refused_before_the_need_test(self) -> None:
        self.use_config(self.NO_PLACEHOLDER)
        _write(self.repo, "scratch.txt", "dirty\n")
        code, _, err, runner = self.run_gate()
        self.assertEqual(code, 2)
        self.assertIn("dirty", err)
        self.assertEqual(runner.calls, [])

    # SPEC: gate#need-unreadable-base-runs
    def test_an_unreadable_base_warns_and_runs(self) -> None:
        # A shallow CI checkout holds no `origin/main`.
        self.use_config(self.NO_PLACEHOLDER)
        code, _, err, runner = self.run_gate(base="origin/main")
        self.assertEqual(code, 0)
        self.assertIn("warning", err)
        self.assertIn("needed", err)
        self.assertEqual(runner.calls, [["bin/test"], ["bin/lint"]])


# `CONFIG` with a second item, `other`, in the scope.
TWO_ITEMS = CONFIG.replace('scope = ["svc", "inputs"]', 'scope = ["svc", "other", "inputs"]') + (
    '[other]\npaths = "other/**"\nunittest_items = "other"\n'
)
# The gate's commands when the diff selects only `other`.
OTHER_ONLY = [["bin/test", "--dirs", "other", "--files", ""], ["bin/lint"]]
# The gate's commands for the whole branch, from `self.base`.
BOTH = [["bin/test", "--dirs", "other,svc", "--files", "svc/a.py"], ["bin/lint"]]


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
        """HEAD's record."""
        return json.loads(_git(self.repo, "cat-file", "blob", f"refs/tangier/gates/backend/{self.key(TWO_ITEMS)}"))

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
        _ = self.run_gate()
        self.assertEqual(self.tangier("gate", "push")[0], 0)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _ = subprocess.run(["git", "clone", "-q", origin, tmp.name], capture_output=True, check=True)
        _ = _commit(tmp.name, "other/b.py", "b = 2\n")
        _, _, runner = self.run_gate(cwd=tmp.name)
        self.assertEqual(runner.calls, OTHER_ONLY)

    # SPEC: gate#run-force
    def test_force_diffs_from_the_merge_base(self) -> None:
        _ = self.run_gate()
        _ = _commit(self.repo, "other/b.py", "b = 2\n")
        _, _, runner = self.run_gate("--force")
        self.assertEqual(runner.calls, [["bin/test", "--dirs", "other,svc", "--files", "svc/a.py"], ["bin/lint"]])


class TestRunMany(GateCase):
    SECOND = CONFIG + '[gate.lint]\ncmd = "bin/lint --all"\nscope = "svc"\n'

    def setUp(self) -> None:
        super().setUp()
        _ = _commit(self.repo, "pipeline.toml", self.SECOND)

    # SPEC: gate#run-all
    def test_all_runs_every_gate_in_name_order(self) -> None:
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
            code, _, err = self.tangier("gate", "run", "backend", "--base", self.base)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.tangier("gate", "push")[0], 0)
        return self.key()

    # SPEC: gate#verified-local-then-origin
    # SPEC: gate#push
    def test_a_pushed_record_is_verified_from_a_second_clone(self) -> None:
        other = self.clone()
        args = ("gate", "verified", "backend")
        self.assertEqual(self.tangier(*args, cwd=other)[:2], (1, "unverified\n"))

        _ = self.tangier("gate", "run", "backend", "--base", self.base)
        # Local only: the record has not left the first clone.
        self.assertEqual(self.tangier(*args)[:2], (0, "verified\n"))
        self.assertEqual(self.tangier(*args, cwd=other)[:2], (1, "unverified\n"))

        self.assertEqual(self.tangier("gate", "push")[0], 0)
        self.assertEqual(self.tangier(*args, cwd=other)[:2], (0, "verified\n"))

    # SPEC: gate#run-reuses-record
    def test_run_reuses_a_record_found_on_origin(self) -> None:
        _ = self.record_and_push(datetime.now(UTC))
        runner = RecordingRunner()
        code, out, _ = self.tangier("gate", "run", "backend", "--base", self.base, runner=runner, cwd=self.clone())
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [])
        self.assertIn("origin", out)

    # SPEC: gate#push
    def test_push_with_no_local_record_succeeds(self) -> None:
        self.assertEqual(self.tangier("gate", "push")[0], 0)
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

    # SPEC: gate#status-values
    def test_a_record_at_head_is_verified_before_the_need_test(self) -> None:
        # The same empty diff, but HEAD's content has a record.
        self.assertEqual(self.tangier("gate", "run", "backend", "--base", "HEAD", "--force")[0], 0)
        _, out, _ = self.tangier("gate", "github-outputs", "--base", "HEAD")
        self.assertIn("backend-status=verified\n", out)

    # SPEC: gate#github-outputs
    def test_github_outputs_reads_origin_once_for_all_gates(self) -> None:
        second = CONFIG + '[gate.lint]\ncmd = "bin/lint"\nscope = "svc"\n'
        _ = _commit(self.repo, "pipeline.toml", second)
        with mock.patch.object(git, "ls_remote", wraps=git.ls_remote) as ls_remote:
            _, out, _ = self.tangier("gate", "github-outputs", "--base", self.base)
        self.assertEqual(ls_remote.call_count, 1)
        self.assertEqual([line.split("=")[0] for line in out.splitlines()][::4], ["backend-status", "lint-status"])

    # SPEC: gate#origin-unreachable
    def test_an_unreachable_origin_is_not_verified(self) -> None:
        _ = _git(self.repo, "remote", "set-url", "origin", os.path.join(self.origin, "gone"))
        code, out, err = self.tangier("gate", "verified", "backend")
        self.assertEqual((code, out), (1, "unverified\n"))
        self.assertIn("warning", err)

    # SPEC: gate#prune-by-record-time
    # SPEC: gate#prune-skips-unreadable
    def test_prune_deletes_old_records_on_origin_by_their_time(self) -> None:
        old = self.record_and_push(datetime(2026, 1, 1, tzinfo=UTC))
        _ = _commit(self.repo, "svc/a.py", "a = 3\n")
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
        _ = _commit(self.repo, "svc/a.py", "a = 3\n")
        _, out, _ = self.tangier("changemap", "build-matrix", "--base", first)
        self.assertEqual(out, 'build-packages=["svc"]\nbuild-packages-empty=false\n')
