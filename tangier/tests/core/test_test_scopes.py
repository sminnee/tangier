"""Every tangier module sits in a test group's scope, or no group re-runs when it changes."""

import os
import unittest

from tangier.config import read_config
from tangier.globs import matches

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class TestTestScopes(unittest.TestCase):
    def test_every_module_is_in_a_src_tag(self) -> None:
        cfg = read_config(os.path.join(ROOT, "pipeline.toml"))
        src_globs = [g for tag, globs in cfg.paths.items() if tag.endswith("-src") for g in globs]
        package = os.path.join(ROOT, "tangier")
        modules = [
            os.path.relpath(os.path.join(d, f), ROOT)
            for d, _, files in os.walk(package)
            if not os.path.relpath(d, package).startswith("tests")
            for f in files
            if f.endswith(".py")
        ]
        self.assertTrue(modules)
        self.assertEqual([m for m in sorted(modules) if not matches(m, src_globs)], [])
