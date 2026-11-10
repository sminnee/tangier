"""Per-job CPU and memory limits, from `[jobs] cpus` and `memory`. Linux only. See `docs/specs/gate.md#jobs`."""

from __future__ import annotations

import os
import re

from tangier.gate import GateError
from tangier.runner import Runner

_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
MIN_MEMORY = 1024**2


def parse_memory(value: object) -> int:
    """Bytes, from an int or a string such as `8G`, with a `K`, `M`, `G` or `T` suffix in powers of 1024.

    Raises ValueError for anything else, or a value below 1M.
    """
    if isinstance(value, bool):
        raise ValueError(f"not a size: {value!r}")
    if isinstance(value, int):
        size = value
    elif isinstance(value, str) and (m := re.fullmatch(r"\s*(\d+)\s*([KMGT])?\s*", value.upper())):
        size = int(m.group(1)) * _UNITS.get(m.group(2) or "", 1)
    else:
        raise ValueError(f"not a size: {value!r}")
    if size < MIN_MEMORY:
        raise ValueError(f"{value!r} is below 1M")
    return size


def show(size: int) -> str:
    """A memory limit as a user would write it: `8G`, `512M`, or bytes when no unit divides it."""
    for unit in ("T", "G", "M", "K"):
        if size % _UNITS[unit] == 0:
            return f"{size // _UNITS[unit]}{unit}"
    return str(size)


def cpu_set(slot: int, n: int, allowed: list[int]) -> set[int] | None:
    """The CPUs slot `slot` runs on: `n` of `allowed`, from `(slot × n) mod len(allowed)`, wrapping.

    None, for no pinning, when `n` is as many as `allowed` holds or more.
    """
    total = len(allowed)
    if n >= total:
        return None
    start = (slot * n) % total
    return {allowed[(start + k) % total] for k in range(n)}


def pin(slot: int, n: int) -> None:
    """Pin this process, and what it starts from now on, to slot `slot`'s `n` CPUs."""
    try:
        cpus = cpu_set(slot, n, sorted(os.sched_getaffinity(0)))
        if cpus is not None:
            os.sched_setaffinity(0, cpus)
    except OSError as e:
        raise GateError(f"cannot pin the job to {n} CPUs: {e}") from e


def wrap(argv: list[str], memory: int | None) -> list[str]:
    """`argv` under a systemd scope that caps its process tree at `memory` bytes, with no swap. `argv` for None.

    `--scope` execs `argv` in place, so its pid and process group stay the job's.
    `OOMPolicy=continue`: the kernel kills the process over the limit, and systemd
    leaves the rest of the scope, the job process with it, to carry on.
    """
    if memory is None:
        return argv
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        *("-p", f"MemoryMax={memory}"),
        *("-p", "MemorySwapMax=0"),
        *("-p", "OOMPolicy=continue"),
        "--",
        *argv,
    ]


def check(runner: Runner, memory: int) -> None:
    """Raise a GateError unless a scope with the memory cap can start here."""
    if runner.which("systemd-run") is None:
        result_text = "systemd-run is not installed"
    else:
        result = runner.run(wrap(["true"], memory))
        if result.ok:
            return
        result_text = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
    raise GateError(
        f"[jobs] memory is set, but a job cannot be capped: {result_text}. "
        "The cap needs a systemd user session (`systemd-run --user --scope`); "
        "start one, or remove `memory` from [jobs] in ~/.tangier/config.toml"
    )

