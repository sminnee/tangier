"""`bin/release-bump` moves the version in every file that names it, or in none."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import tangier
from tangier.tests.support import REPO_ROOT

SCRIPTS = ["bin/release-bump", "bin/release_version.py"]
FILES = {
    "pyproject.toml": '[project]\nname = "tangier"\nversion = "{}"\n',
    "tangier/__init__.py": '"""tangier."""\n\n__version__ = "{}"\n',
    "skills/tangier/reference/cli.md": "`tangier --version` prints it, as `tangier {}`.\n",
    "skills/tangier/reference/github-actions.md": "Pin `@v0`, or an exact `@v{}`.\n",
}


def _run_bump(repo: str, arg: str) -> subprocess.CompletedProcess[str]:
    # `sys.executable`: each test gate member runs the script on its own Python.
    return subprocess.run(
        [sys.executable, os.path.join(repo, "bin", "release-bump"), arg], capture_output=True, text=True
    )


class TestReleaseBump(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo)
        os.makedirs(os.path.join(self.repo, "bin"))
        for script in SCRIPTS:
            _ = shutil.copy(os.path.join(REPO_ROOT, script), os.path.join(self.repo, script))
        self.write("0.3.2")

    def write(self, version: str) -> None:
        for path, body in FILES.items():
            os.makedirs(os.path.dirname(os.path.join(self.repo, path)), exist_ok=True)
            with open(os.path.join(self.repo, path), "w") as fh:
                _ = fh.write(body.format(version))

    def read(self) -> dict[str, str]:
        out = {}
        for path in FILES:
            with open(os.path.join(self.repo, path)) as fh:
                out[path] = fh.read()
        return out

    def assert_bumps(self, arg: str, expected: str) -> None:
        result = _run_bump(self.repo, arg)
        self.assertEqual(
            (result.returncode, result.stdout), (0, f"version={expected}\nprevious=0.3.2\n"), result.stderr
        )
        self.assertEqual(self.read(), {path: body.format(expected) for path, body in FILES.items()})

    def assert_refused(self, arg: str, message: str) -> None:
        before = self.read()
        result = _run_bump(self.repo, arg)
        self.assertEqual(result.returncode, 1)
        self.assertIn(message, result.stderr)
        self.assertEqual(self.read(), before)

    def test_patch(self) -> None:
        self.assert_bumps("patch", "0.3.3")

    def test_minor(self) -> None:
        self.assert_bumps("minor", "0.4.0")

    def test_major(self) -> None:
        self.assert_bumps("major", "1.0.0")

    def test_an_explicit_version_above_the_current_one(self) -> None:
        self.assert_bumps("0.10.0", "0.10.0")

    def test_an_explicit_version_below_the_current_one_is_refused(self) -> None:
        self.assert_refused("0.3.1", "not above the current version 0.3.2")

    def test_the_current_version_is_refused(self) -> None:
        self.assert_refused("0.3.2", "not above the current version 0.3.2")

    def test_a_pre_release_version_is_refused(self) -> None:
        self.assert_refused("0.4.0rc1", "must look like 1.2.3")

    def test_a_file_without_the_version_changes_no_file(self) -> None:
        with open(os.path.join(self.repo, "skills/tangier/reference/github-actions.md"), "w") as fh:
            _ = fh.write("Pin `@v0`.\n")
        self.assert_refused("patch", "github-actions.md names `@v0.3.2` 0 times")

    def test_a_file_naming_the_version_twice_changes_no_file(self) -> None:
        with open(os.path.join(self.repo, "skills/tangier/reference/cli.md"), "a") as fh:
            _ = fh.write("Again: `tangier 0.3.2`.\n")
        self.assert_refused("minor", "cli.md names `tangier 0.3.2` 2 times")

    def test_differing_package_versions_are_refused(self) -> None:
        with open(os.path.join(self.repo, "tangier/__init__.py"), "w") as fh:
            _ = fh.write('__version__ = "0.3.1"\n')
        self.assert_refused("patch", "the package versions differ")


class TestReleaseBumpTargets(unittest.TestCase):
    """A docs edit that drops the version would break the next automated bump."""

    def test_the_real_files_bump(self) -> None:
        repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo)
        for path in [*FILES, *SCRIPTS]:
            os.makedirs(os.path.dirname(os.path.join(repo, path)), exist_ok=True)
            _ = shutil.copy(os.path.join(REPO_ROOT, path), os.path.join(repo, path))
        major, minor, patch = tangier.__version__.split(".")
        result = _run_bump(repo, "patch")
        self.assertEqual(
            (result.returncode, result.stdout),
            (0, f"version={major}.{minor}.{int(patch) + 1}\nprevious={tangier.__version__}\n"),
            result.stderr,
        )
