"""Shared test helpers.

`make_config` builds TOML and runs it through the real parser rather than
constructing a `Config` by hand. A hand-built helper is a parallel
reimplementation of the parser that drifts from it — and a drifted helper
proves nothing about the code that actually runs.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
from typing import Any

from tangier.config import Config, read_config
from tangier.runner import Result


class Raw(str):
    """A TOML fragment to emit verbatim, for syntax the renderer can't express.

    Use it for inline tables: `Raw('{ cmd = "bin/test" }')`.
    """


def _toml_value(val: Any) -> str:
    if isinstance(val, Raw):
        return str(val)
    if val is True:
        return "true"
    if val is False:
        return "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, str):
        return f'"{val}"'
    if isinstance(val, list):
        return "[" + ", ".join(_toml_value(v) for v in val) + "]"
    raise TypeError(f"unsupported TOML value: {val!r}")


def to_toml(tables: dict[str, dict[str, Any]]) -> str:
    """Render {table: {key: value}} as TOML text."""
    out: list[str] = []
    for name, body in tables.items():
        out.append(f"[{name}]")
        for key, val in body.items():
            out.append(f"{key} = {_toml_value(val)}")
        out.append("")
    return "\n".join(out)


def parse_toml(body: str) -> Config:
    """Parse TOML text through the real reader, swallowing parse warnings."""
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        _ = fh.write(body)
        path = fh.name
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return read_config(path)
    finally:
        os.unlink(path)


def parse_toml_stderr(body: str) -> str:
    """Parse TOML text and return whatever warnings went to stderr."""
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        _ = fh.write(body)
        path = fh.name
    try:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            _ = read_config(path)
        return stderr.getvalue()
    finally:
        os.unlink(path)


def make_config(**tables: dict[str, Any]) -> Config:
    """Build a Config from table definitions, via TOML and the real parser.

    Keyword names are table names; use `**{"unittest-files": {...}}` for names
    that aren't valid Python identifiers.
    """
    return parse_toml(to_toml(tables))


def make_git_repo(testcase: Any, files: dict[str, str]) -> str:
    """Create a throwaway git repo containing `files`, returning its path."""
    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    root = tmp.name
    for rel, content in files.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            _ = fh.write(content)
    for cmd in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
    ):
        _ = subprocess.run(["git", "-C", root, *cmd], capture_output=True, check=True)
    return root


class RecordingRunner:
    """A Runner that records calls and replays canned responses.

    `responses` maps an argv **prefix tuple** to either a Result or a list of
    Results consumed in order — so `rollout status` can fail twice then succeed.
    Longest matching prefix wins, so a specific rule beats a general one.

    `slept` accumulates the values passed to `sleep`, which is how the poll
    loop's clock is faked: a 600s timeout costs microseconds and no test needs
    to import `time`.
    """

    def __init__(self, responses: dict[tuple[str, ...], Any] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.pipes: list[list[list[str]]] = []
        self.stdins: list[str | None] = []
        self.slept: list[float] = []
        self.responses = dict(responses or {})
        self.missing: set[str] = set()

    def _lookup(self, argv: list[str]) -> Result:
        best: tuple[int, Any] | None = None
        for prefix, response in self.responses.items():
            if tuple(argv[: len(prefix)]) == prefix and (best is None or len(prefix) > best[0]):
                best = (len(prefix), response)
        if best is None:
            return Result(0)
        response = best[1]
        if isinstance(response, list):
            if not response:
                return Result(0)
            # A queued sequence: consume one entry per call, then hold the last.
            return response.pop(0) if len(response) > 1 else response[0]
        return response

    def run(
        self,
        argv: list[str],
        *,
        input: str | None = None,
        capture: bool = True,
        env: dict[str, str] | None = None,
        check: bool = False,
    ) -> Result:
        self.calls.append(list(argv))
        self.stdins.append(input)
        return self._lookup(argv)

    def pipe(self, stages: list[list[str]], *, input: str | None = None, env: dict[str, str] | None = None) -> Result:
        self.pipes.append([list(s) for s in stages])
        for stage in stages:
            self.calls.append(list(stage))
        self.stdins.append(input)
        return self._lookup(stages[-1] if stages else [])

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def which(self, program: str) -> str | None:
        return None if program in self.missing else f"/usr/bin/{program}"

    def commands(self, *, binary: str | None = None) -> list[str]:
        """Recorded calls as joined strings, optionally filtered by binary."""
        return [" ".join(c) for c in self.calls if binary is None or (c and c[0] == binary)]
