"""A gate's JUnit XML report: its test counts, and which tests failed. See `docs/specs/gate.md#junit-report`."""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass

# The most failing tests one run keeps. The counts still cover every test.
MAX_FAILURES = 100

# A suite name with no `/` names a file when it is one name with a source extension, as `app.py`.
# The list keeps a module or class name, as `auth.utils` or `Login.spec`, out of the files.
_SOURCE_FILE = re.compile(
    r"[^\s./]+\.(py|pyi|js|jsx|mjs|cjs|ts|tsx|mts|cts|vue|svelte|go|rb|rs|java|kt|kts|scala|php|cs|swift|c|cc|cpp|h|hpp|m)"
)


@dataclass
class Report:
    tests: int
    failed: int
    errored: int
    skipped: int
    # Each failing test, at most `MAX_FAILURES`, in report order.
    failures: list[dict[str, object]]

    def counts(self) -> dict[str, int]:
        return {"tests": self.tests, "failures": self.failed, "errors": self.errored, "skipped": self.skipped}


def read(path: str) -> Report | None:
    """The report at `path`. None, with a warning on stderr, when it is missing or cannot be parsed."""
    try:
        root = ET.parse(path).getroot()
    except FileNotFoundError:
        print(f"warning: no JUnit report at {path}; the run is recorded without one", file=sys.stderr)
        return None
    except (OSError, ET.ParseError) as e:
        print(f"warning: cannot read the JUnit report at {path} ({e}); the run is recorded without it", file=sys.stderr)
        return None
    if root.tag not in ("testsuites", "testsuite"):
        print(
            f"warning: {path} is not a JUnit report (its root is <{root.tag}>); the run is recorded without it",
            file=sys.stderr,
        )
        return None
    report = Report(0, 0, 0, 0, [])
    _walk(root, None, report)
    return report


def _walk(element: ET.Element, suite: str | None, report: Report) -> None:
    """Count each `<testcase>` under `element`. `suite` is the nearest enclosing suite's name."""
    if element.tag == "testsuite":
        suite = element.get("name") or suite
    for child in element:
        if child.tag == "testcase":
            _case(child, suite, report)
        elif child.tag in ("testsuite", "testsuites"):
            _walk(child, suite, report)


def _case(case: ET.Element, suite: str | None, report: Report) -> None:
    report.tests += 1
    found = case.find("failure")
    outcome = "failure"
    if found is None:
        found, outcome = case.find("error"), "error"
    if found is None:
        if case.find("skipped") is not None:
            report.skipped += 1
        return
    if outcome == "failure":
        report.failed += 1
    else:
        report.errored += 1
    if len(report.failures) >= MAX_FAILURES:
        return
    entry: dict[str, object] = {}
    for field, value in (("suite", suite), ("classname", case.get("classname")), ("test", case.get("name"))):
        if value:
            entry[field] = value
    file = case.get("file") or (suite if suite and ("/" in suite or _SOURCE_FILE.fullmatch(suite)) else None)
    if file:
        entry["file"] = file
    line = case.get("line")
    if line and line.isdigit():
        entry["line"] = int(line)
    entry["outcome"] = outcome
    if kind := found.get("type"):
        entry["type"] = kind
    report.failures.append(entry)
