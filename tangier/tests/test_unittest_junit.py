"""`bin/unittest-junit`: the test gates' exit code, and the report `junit.read` takes from it."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

from tangier import junit

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "bin",
    "unittest-junit",
)

SUITE = """\
import unittest


class T(unittest.TestCase):
    def test_pass(self):
        pass

    def test_fail(self):
        self.assertEqual(1, 2)

    def test_error(self):
        raise KeyError("x")

    @unittest.skip("slow")
    def test_skip(self):
        pass

    def test_subtests(self):
        for i in range(3):
            with self.subTest(i):
                self.assertEqual(i, 0)
"""


class TestUnittestJunit(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def run_suite(self, source: str | None) -> tuple[int, junit.Report | None]:
        if source is not None:
            with open(os.path.join(self.dir, "test_x.py"), "w") as fh:
                _ = fh.write(source)
        done = subprocess.run(
            [sys.executable, SCRIPT, "build/report.xml", "-s", ".", "-p", "test_*.py"],
            cwd=self.dir,
            capture_output=True,
            text=True,
        )
        with contextlib.chdir(self.dir), contextlib.redirect_stderr(io.StringIO()):
            return done.returncode, junit.read("build/report.xml")

    # SPEC: gate#junit-report
    def test_a_failing_suite_exits_1_and_reports_each_outcome(self) -> None:
        code, report = self.run_suite(SUITE)
        self.assertEqual(code, 1)
        assert report is not None
        self.assertEqual(report.counts(), {"tests": 5, "failures": 2, "errors": 1, "skipped": 1})
        # A test whose subtests fail is one entry.
        self.assertEqual(
            report.failures,
            [
                {
                    "suite": "unittest",
                    "classname": "test_x.T",
                    "test": "test_error",
                    "file": "test_x.py",
                    "line": 11,
                    "outcome": "error",
                    "type": "KeyError",
                },
                {
                    "suite": "unittest",
                    "classname": "test_x.T",
                    "test": "test_fail",
                    "file": "test_x.py",
                    "line": 8,
                    "outcome": "failure",
                    "type": "AssertionError",
                },
                {
                    "suite": "unittest",
                    "classname": "test_x.T",
                    "test": "test_subtests",
                    "file": "test_x.py",
                    "line": 18,
                    "outcome": "failure",
                    "type": "AssertionError",
                },
            ],
        )

    def test_a_passing_suite_exits_0(self) -> None:
        code, report = self.run_suite(
            "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n"
        )
        self.assertEqual(code, 0)
        assert report is not None
        self.assertEqual(report.counts(), {"tests": 1, "failures": 0, "errors": 0, "skipped": 0})

    def test_no_tests_exits_5_as_unittest_does(self) -> None:
        code, report = self.run_suite(None)
        self.assertEqual(code, 5)
        assert report is not None
        self.assertEqual(report.tests, 0)
