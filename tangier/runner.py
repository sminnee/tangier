"""The subprocess seam.

Every external command tangier runs goes through a Runner, so tests can assert
call sequences without touching a registry or a cluster. This is the only module
permitted to import `subprocess` or `time`.

Four methods, each naming a capability the ported shell actually uses and none
expressible by the others:

  run    one command
  pipe   a chain of commands, shell-pipeline semantics
  sleep  the poll loop's clock — faked in tests so a 600s timeout costs microseconds
  which  turn a missing binary into a clean error instead of an OSError
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Runner(Protocol):
    """The seam. `check=False` by default, mirroring the ported bash's missing `set -e`."""

    def run(
        self,
        argv: list[str],
        *,
        input: str | None = None,
        capture: bool = True,
        env: dict[str, str] | None = None,
        check: bool = False,
    ) -> Result: ...

    def pipe(
        self, stages: list[list[str]], *, input: str | None = None, env: dict[str, str] | None = None
    ) -> Result: ...

    def sleep(self, seconds: float) -> None: ...

    def which(self, program: str) -> str | None: ...


class CommandFailed(RuntimeError):
    """Raised by `run(..., check=True)`."""

    def __init__(self, argv: list[str], result: Result) -> None:
        super().__init__(f"command failed (exit {result.returncode}): {' '.join(argv)}")
        self.argv = argv
        self.result = result


class Subprocess:
    """The real runner.

    `echo=True` prints each command before running it, preserving
    build-github's echo-then-run behaviour so CI logs still show the buildx line.
    """

    def __init__(self, *, echo: bool = False) -> None:
        self.echo = echo

    def run(
        self,
        argv: list[str],
        *,
        input: str | None = None,
        capture: bool = True,
        env: dict[str, str] | None = None,
        check: bool = False,
    ) -> Result:
        if self.echo:
            print(" ".join(argv))
        proc = subprocess.run(
            argv,
            input=input,
            capture_output=capture,
            text=True,
            env=env,
            check=False,
        )
        result = Result(proc.returncode, proc.stdout or "" if capture else "", proc.stderr or "" if capture else "")
        if check and result.returncode != 0:
            raise CommandFailed(argv, result)
        return result

    def pipe(self, stages: list[list[str]], *, input: str | None = None, env: dict[str, str] | None = None) -> Result:
        """Chain `stages` with pipes, returning the last stage's result.

        Failure semantics match a shell without `pipefail`: only the final
        stage's returncode is reported. Implemented by chaining Popen — never
        `shell=True`, so no argument ever passes through a shell parser.
        """
        if not stages:
            return Result(0)
        procs: list[subprocess.Popen[str]] = []
        first = subprocess.Popen(
            stages[0],
            stdin=subprocess.PIPE if input is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        procs.append(first)
        for stage in stages[1:]:
            nxt = subprocess.Popen(
                stage,
                stdin=procs[-1].stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            # Let the upstream process see EPIPE if the downstream one exits.
            if procs[-1].stdout is not None:
                procs[-1].stdout.close()
            procs.append(nxt)
        if input is not None and first.stdin is not None:
            first.stdin.write(input)
            first.stdin.close()
        stdout, stderr = procs[-1].communicate()
        for p in procs[:-1]:
            _ = p.wait()
        return Result(procs[-1].returncode, stdout or "", stderr or "")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def which(self, program: str) -> str | None:
        return shutil.which(program)


class DryRun:
    """Prints what would run and reports success without running it."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

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
        print("would run: " + " ".join(argv))
        return Result(0)

    def pipe(self, stages: list[list[str]], *, input: str | None = None, env: dict[str, str] | None = None) -> Result:
        for stage in stages:
            self.calls.append(list(stage))
        print("would run: " + " | ".join(" ".join(s) for s in stages))
        return Result(0)

    def sleep(self, seconds: float) -> None:
        return None

    def which(self, program: str) -> str | None:
        return f"/usr/bin/{program}"
