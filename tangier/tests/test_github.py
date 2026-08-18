"""The `$GITHUB_OUTPUT` / `$GITHUB_STEP_SUMMARY` writers.

Both must work off a runner, since every command using them is also a local
debugging tool.
"""

import contextlib
import io
import os
import tempfile
import unittest
import unittest.mock

from tangier.github import emit_outputs, write_summary


class TestEmitOutputs(unittest.TestCase):
    def test_echoes_to_stdout_when_no_output_file(self) -> None:
        buf = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(buf):
            emit_outputs({"tag": "abc123", "built": "true"})
        self.assertEqual(buf.getvalue(), "tag=abc123\nbuilt=true\n")

    def test_appends_to_the_output_file_and_still_echoes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.txt")
            buf = io.StringIO()
            with (
                unittest.mock.patch.dict(os.environ, {"GITHUB_OUTPUT": path}),
                contextlib.redirect_stdout(buf),
            ):
                emit_outputs({"tag": "abc123"})
                emit_outputs({"built": "true"})
            with open(path) as fh:
                self.assertEqual(fh.read(), "tag=abc123\nbuilt=true\n")
            self.assertEqual(buf.getvalue(), "tag=abc123\nbuilt=true\n")


class TestWriteSummary(unittest.TestCase):
    def test_returns_false_when_unset(self) -> None:
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(write_summary("## Table\n"))

    def test_appends_and_returns_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "summary.md")
            with unittest.mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": path}):
                self.assertTrue(write_summary("## One"))
                self.assertTrue(write_summary("## Two\n"))
            with open(path) as fh:
                self.assertEqual(fh.read(), "## One\n## Two\n")
