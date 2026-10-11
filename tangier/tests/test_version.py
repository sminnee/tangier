"""`tangier.__version__` has no build step to set it, so a test keeps it equal to `pyproject.toml`."""

import contextlib
import io
import os
import tomllib
import unittest

import tangier
from tangier import cli


class TestVersion(unittest.TestCase):
    def test_version_matches_pyproject(self) -> None:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, "pyproject.toml"), "rb") as fh:
            self.assertEqual(tangier.__version__, tomllib.load(fh)["project"]["version"])

    def test_version_flag_prints_it_without_a_config(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            _ = cli.main(["--config", "/nonexistent/pipeline.toml", "--version"])
        self.assertEqual((raised.exception.code, out.getvalue()), (0, f"tangier {tangier.__version__}\n"))
