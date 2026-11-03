"""`bin/release-version` works out the next release from the tags, and stamps it into the build."""

from __future__ import annotations

import os
import subprocess
import unittest

from tangier.tests.support import REPO_ROOT, make_git_repo

SCRIPT = os.path.join(REPO_ROOT, "bin", "release-version")

PYPROJECT = '[project]\nname = "tangier"\nversion = "0.0.0.dev0"\n\n[tool.ruff]\ntarget-version = "py311"\n'
MODULE = '"""tangier."""\n\n__version__ = "0.0.0.dev0"\n'


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class TestReleaseVersion(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = make_git_repo(self, {"pyproject.toml": PYPROJECT, "tangier/__init__.py": MODULE})

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([SCRIPT, *args], cwd=self.repo, capture_output=True, text=True)

    def next(self, bump: str) -> dict[str, str]:
        result = self.run_script("next", bump)
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def tag(self, tag: str) -> None:
        _ = _git(self.repo, "tag", "-a", tag, "-m", tag)
        _ = _git(self.repo, "commit", "-q", "--allow-empty", "-m", f"after {tag}")

    def read(self, path: str) -> str:
        with open(os.path.join(self.repo, path)) as fh:
            return fh.read()

    def test_each_bump_moves_the_highest_tag(self) -> None:
        self.tag("v0.2.9")
        self.tag("v0.3.1")
        self.tag("v0.10.0rc1")
        self.assertEqual(
            [self.next(bump) for bump in ("patch", "minor", "major")],
            [
                {"version": "0.3.2", "previous": "0.3.1"},
                {"version": "0.4.0", "previous": "0.3.1"},
                {"version": "1.0.0", "previous": "0.3.1"},
            ],
        )

    def test_an_exact_version_above_the_highest_tag_is_taken(self) -> None:
        self.tag("v0.3.1")
        self.assertEqual(self.next("0.5.0"), {"version": "0.5.0", "previous": "0.3.1"})

    def test_an_exact_version_not_above_the_highest_tag_is_refused(self) -> None:
        self.tag("v0.3.1")
        result = self.run_script("next", "0.3.1")
        self.assertEqual(
            (result.returncode, result.stderr), (1, "error: 0.3.1 is not above the highest release 0.3.1\n")
        )

    def test_a_pre_release_version_is_refused(self) -> None:
        result = self.run_script("next", "0.4.0rc1")
        self.assertEqual(
            (result.returncode, result.stderr), (1, "error: version must look like 1.2.3, got: 0.4.0rc1\n")
        )

    def test_a_first_release_has_no_previous(self) -> None:
        self.assertEqual(self.next("patch"), {"version": "0.0.1", "previous": ""})

    def test_a_released_head_is_refused(self) -> None:
        _ = _git(self.repo, "tag", "-a", "v0.3.1", "-m", "v0.3.1")
        result = self.run_script("next", "patch")
        self.assertEqual((result.returncode, result.stderr), (1, "error: HEAD is already released as v0.3.1\n"))

    def test_stamp_writes_the_version_into_both_files(self) -> None:
        result = self.run_script("stamp", "0.3.2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.read("pyproject.toml"), self.read("tangier/__init__.py")),
            (PYPROJECT.replace("0.0.0.dev0", "0.3.2"), MODULE.replace("0.0.0.dev0", "0.3.2")),
        )

    def test_stamp_changes_no_file_when_one_lacks_the_version(self) -> None:
        with open(os.path.join(self.repo, "tangier", "__init__.py"), "w") as fh:
            _ = fh.write('"""tangier."""\n')
        result = self.run_script("stamp", "0.3.2")
        self.assertEqual(
            (result.returncode, result.stderr, self.read("pyproject.toml")),
            (1, "error: tangier/__init__.py names a version 0 times, expected once\n", PYPROJECT),
        )
