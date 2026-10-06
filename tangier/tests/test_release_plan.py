"""`bin/release-plan` decides whether a commit on main releases, and so whether `@v0` moves."""

from __future__ import annotations

import os
import subprocess
import unittest

from tangier.tests.support import make_git_repo

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "bin",
    "release-plan",
)


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit_version(repo: str, version: str, module_version: str | None = None) -> str:
    """Commit `version` to both version files, returning the new commit."""
    with open(os.path.join(repo, "pyproject.toml"), "w") as fh:
        _ = fh.write(f'[project]\nname = "tangier"\nversion = "{version}"\n')
    with open(os.path.join(repo, "tangier", "__init__.py"), "w") as fh:
        _ = fh.write(f'__version__ = "{module_version or version}"\n')
    _ = _git(repo, "commit", "-qam", version, "--allow-empty")
    return _git(repo, "rev-parse", "HEAD")


def _tag(repo: str, tag: str, commit: str) -> None:
    _ = _git(repo, "tag", "-a", tag, "-m", tag, commit)


class TestReleasePlan(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = make_git_repo(self, {"pyproject.toml": "", "tangier/__init__.py": ""})

    def plan(self, commit: str) -> dict[str, str]:
        result = subprocess.run([SCRIPT, commit], cwd=self.repo, capture_output=True, text=True, check=True)
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def test_a_first_version_with_no_tags_releases(self) -> None:
        head = _commit_version(self.repo, "0.1.0")
        self.assertEqual(self.plan(head), {"version": "0.1.0", "release": "true"})

    def test_a_new_version_above_the_highest_tag_releases(self) -> None:
        _tag(self.repo, "v0.2.1", _commit_version(self.repo, "0.2.1"))
        head = _commit_version(self.repo, "0.2.2")
        self.assertEqual(self.plan(head), {"version": "0.2.2", "release": "true"})

    def test_the_highest_tagged_commit_releases_again(self) -> None:
        head = _commit_version(self.repo, "0.2.1")
        _tag(self.repo, "v0.2.1", head)
        self.assertEqual(self.plan(head), {"version": "0.2.1", "release": "true"})

    def test_an_older_tagged_commit_does_not_release(self) -> None:
        old = _commit_version(self.repo, "0.2.0")
        _tag(self.repo, "v0.2.0", old)
        _tag(self.repo, "v0.2.1", _commit_version(self.repo, "0.2.1"))
        self.assertEqual(self.plan(old), {"version": "0.2.0", "release": "false"})

    def test_an_unchanged_version_does_not_release(self) -> None:
        _tag(self.repo, "v0.2.1", _commit_version(self.repo, "0.2.1"))
        head = _commit_version(self.repo, "0.2.1")
        self.assertEqual(self.plan(head), {"version": "0.2.1", "release": "false"})

    def test_an_untagged_version_below_the_highest_does_not_release(self) -> None:
        superseded = _commit_version(self.repo, "0.2.2")
        _tag(self.repo, "v0.2.3", _commit_version(self.repo, "0.2.3"))
        self.assertEqual(self.plan(superseded), {"version": "0.2.2", "release": "false"})

    def test_a_lightweight_tag_counts(self) -> None:
        head = _commit_version(self.repo, "0.2.1")
        _ = _git(self.repo, "tag", "v0.2.1", head)
        self.assertEqual(self.plan(head), {"version": "0.2.1", "release": "true"})

    def test_a_pre_release_tag_is_ignored(self) -> None:
        _tag(self.repo, "v0.3.0rc1", _commit_version(self.repo, "0.2.9"))
        head = _commit_version(self.repo, "0.3.0")
        self.assertEqual(self.plan(head), {"version": "0.3.0", "release": "true"})

    def test_mismatched_versions_fail(self) -> None:
        head = _commit_version(self.repo, "0.2.2", module_version="0.2.3")
        result = subprocess.run([SCRIPT, head], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("differ", result.stderr)

    def test_a_pre_release_version_fails(self) -> None:
        head = _commit_version(self.repo, "0.3.0rc1")
        result = subprocess.run([SCRIPT, head], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
