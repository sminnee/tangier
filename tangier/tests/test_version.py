"""`tangier.__version__` has no build step to set it, so a test keeps it equal to `pyproject.toml`."""

import os
import tomllib
import unittest

import tangier


class TestVersion(unittest.TestCase):
    def test_version_matches_pyproject(self) -> None:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, "pyproject.toml"), "rb") as fh:
            self.assertEqual(tangier.__version__, tomllib.load(fh)["project"]["version"])
