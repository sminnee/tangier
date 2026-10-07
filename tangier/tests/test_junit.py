"""`tangier.junit`: reading a gate's JUnit XML report, from files the common tools write."""

import contextlib
import io
import os
import tempfile
import unittest

from tangier import junit

PYTEST = """\
<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="1" tests="4">
<testcase classname="tests.test_api.TestLogin" name="test_ok" file="tests/test_api.py" line="10" time="0.1"/>
<testcase classname="tests.test_api.TestLogin" name="test_bad_password" file="tests/test_api.py" line="42" time="0.2">
  <failure message="assert 401 == 403" type="AssertionError">long traceback</failure>
</testcase>
<testcase classname="tests.test_db" name="test_connect" time="0.0">
  <error message="fixture failed">conn refused</error>
</testcase>
<testcase classname="tests.test_db" name="test_slow" time="0.0"><skipped message="slow"/></testcase>
</testsuite></testsuites>
"""

# `ruff check --output-format junit`: a suite per file, a case per violation, named by rule.
RUFF = """\
<?xml version="1.0" encoding="UTF-8"?>
<testsuites name="ruff" tests="2" failures="2" errors="0">
  <testsuite name="src/app.py" tests="1" disabled="0" errors="0" failures="1" package="org.ruff">
    <testcase name="org.ruff.F401" classname="src/app.py" line="3" column="8">
      <failure message="`os` imported but unused" type="F401">line 3, col 8, `os` imported but unused</failure>
    </testcase>
  </testsuite>
  <testsuite name="main.py" tests="1" failures="1">
    <testcase name="org.ruff.E501" classname="main.py" line="9">
      <failure message="Line too long" type="E501">line 9</failure>
    </testcase>
  </testsuite>
</testsuites>
"""

# vitest's junit reporter: a suite per file, with nested describe blocks in the case name, and no `type`.
VITEST = """\
<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="2" failures="1" errors="0">
    <testsuite name="src/sum.test.ts" timestamp="2026-10-01T00:00:00" tests="2" failures="1">
        <testcase classname="src/sum.test.ts" name="sum &gt; adds numbers" time="0.002">
            <failure message="expected 3 to be 4">AssertionError: expected 3 to be 4</failure>
        </testcase>
        <testcase classname="src/sum.test.ts" name="sum &gt; adds zero" time="0.001"/>
    </testsuite>
</testsuites>
"""


class JunitCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def read(self, content: str | None) -> tuple[junit.Report | None, str]:
        path = os.path.join(self.dir, "report.xml")
        if content is not None:
            with open(path, "w") as fh:
                _ = fh.write(content)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            report = junit.read(path)
        return report, err.getvalue()


class TestRead(JunitCase):
    # SPEC: gate#junit-report
    def test_pytest_keeps_each_failure_and_error_without_its_message(self) -> None:
        report, err = self.read(PYTEST)
        assert report is not None
        self.assertEqual(err, "")
        self.assertEqual(report.counts(), {"tests": 4, "failures": 1, "errors": 1, "skipped": 1})
        self.assertEqual(
            report.failures,
            [
                {
                    "suite": "pytest",
                    "classname": "tests.test_api.TestLogin",
                    "test": "test_bad_password",
                    "file": "tests/test_api.py",
                    "line": 42,
                    "outcome": "failure",
                    "type": "AssertionError",
                },
                {"suite": "pytest", "classname": "tests.test_db", "test": "test_connect", "outcome": "error"},
            ],
        )

    # SPEC: gate#junit-report
    def test_ruff_takes_the_file_from_a_suite_named_by_path(self) -> None:
        report, _ = self.read(RUFF)
        assert report is not None
        self.assertEqual(
            [(f["file"], f["test"], f["type"], f["line"]) for f in report.failures],
            [("src/app.py", "org.ruff.F401", "F401", 3), ("main.py", "org.ruff.E501", "E501", 9)],
        )

    # SPEC: gate#junit-report
    def test_vitest_counts_cases_not_the_suite_attributes(self) -> None:
        report, _ = self.read(VITEST.replace('tests="2" failures="1">', 'tests="9" failures="7">'))
        assert report is not None
        self.assertEqual(report.counts(), {"tests": 2, "failures": 1, "errors": 0, "skipped": 0})
        self.assertEqual(
            report.failures,
            [
                {
                    "suite": "src/sum.test.ts",
                    "classname": "src/sum.test.ts",
                    "test": "sum > adds numbers",
                    "file": "src/sum.test.ts",
                    "outcome": "failure",
                }
            ],
        )

    # SPEC: gate#junit-report
    def test_a_bare_testsuite_root_and_nested_suites_are_read(self) -> None:
        nested = (
            '<testsuite name="outer"><testsuite name="lib/inner.py">'
            '<testcase classname="c" name="t"><failure/></testcase></testsuite>'
            '<testcase classname="c" name="u"><failure/></testcase></testsuite>'
        )
        report, _ = self.read(nested)
        assert report is not None
        self.assertEqual(
            [(f["suite"], f.get("file")) for f in report.failures], [("lib/inner.py", "lib/inner.py"), ("outer", None)]
        )

    # SPEC: gate#junit-report
    def test_a_module_or_class_suite_name_is_not_a_file(self) -> None:
        for name in ("com.example.FooTest", "auth.utils", "Login.spec"):
            with self.subTest(name):
                report, _ = self.read(f'<testsuite name="{name}"><testcase name="t"><failure/></testcase></testsuite>')
                assert report is not None
                self.assertNotIn("file", report.failures[0])

    # SPEC: gate#junit-cap
    def test_the_failure_list_is_capped_but_the_counts_are_not(self) -> None:
        cases = "".join(
            f'<testcase classname="c" name="t{i}"><failure/></testcase>' for i in range(junit.MAX_FAILURES + 5)
        )
        report, _ = self.read(f"<testsuites><testsuite name='s'>{cases}</testsuite></testsuites>")
        assert report is not None
        self.assertEqual(len(report.failures), junit.MAX_FAILURES)
        self.assertEqual(report.failed, junit.MAX_FAILURES + 5)
        self.assertEqual(report.failures[-1]["test"], f"t{junit.MAX_FAILURES - 1}")

    # SPEC: gate#junit-unreadable
    def test_a_missing_report_is_a_warning(self) -> None:
        report, err = self.read(None)
        self.assertIsNone(report)
        self.assertIn("warning: no JUnit report at", err)

    # SPEC: gate#junit-unreadable
    def test_a_malformed_report_is_a_warning(self) -> None:
        for label, content in (("not xml", "<testsuites><testcase"), ("not junit", "<html></html>")):
            with self.subTest(label):
                report, err = self.read(content)
                self.assertIsNone(report)
                self.assertIn("the run is recorded without it", err)
