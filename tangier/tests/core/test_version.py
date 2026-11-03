"""`tangier.__version__` and `pyproject.toml` name the same version, which bin/release-version stamps into both."""

import contextlib
import io
import os
import tomllib
import unittest

import tangier
from tangier import cli
from tangier.tests.support import REPO_ROOT


class TestVersion(unittest.TestCase):
    def test_version_matches_pyproject(self) -> None:
        with open(os.path.join(REPO_ROOT, "pyproject.toml"), "rb") as fh:
            self.assertEqual(tangier.__version__, tomllib.load(fh)["project"]["version"])

    def test_version_flag_prints_it_without_a_config(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            _ = cli.main(["--config", "/nonexistent/pipeline.toml", "--version"])
        self.assertEqual((raised.exception.code, out.getvalue()), (0, f"tangier {tangier.__version__}\n"))
