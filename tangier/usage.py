"""The CPU and memory gate jobs use, for `gate status`. See `docs/specs/gate.md#status-usage`.

Each job runs in its own session, so its pid is the process group of every command it starts.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from tangier.runner import Runner


@dataclass(frozen=True)
class Usage:
    """One process group's use. `cpu_percent` is a percentage of one CPU, to 0.1."""

    cpu_percent: float
    rss_bytes: int
    processes: int


def sample(runner: Runner) -> dict[int, Usage]:
    """Each process group's use, by its id. `{}` when `ps` is missing or fails."""
    try:
        result = runner.run(["ps", "-A", "-o", "pgid=,pcpu=,rss="], capture=True)
    except OSError:
        return {}
    if not result.ok:
        return {}
    totals: dict[int, tuple[float, int, int]] = {}
    for line in result.stdout.splitlines():
        try:
            pgid, cpu, rss = line.split()
            group, percent, kib = int(pgid), float(cpu.replace(",", ".")), int(rss)
        except ValueError:
            continue
        old = totals.get(group, (0.0, 0, 0))
        totals[group] = (old[0] + percent, old[1] + kib * 1024, old[2] + 1)
    return {group: Usage(round(cpu, 1), rss, n) for group, (cpu, rss, n) in totals.items()}


def memory_total(runner: Runner) -> int | None:
    """The machine's memory in bytes, or None where it cannot be read."""
    platform: str = sys.platform
    if platform.startswith("linux"):
        try:
            with open("/proc/meminfo") as fh:
                return meminfo_total(fh.read())
        except OSError:
            return None
    if platform == "darwin":
        try:
            result = runner.run(["sysctl", "-n", "hw.memsize"], capture=True)
        except OSError:
            return None
        return int(result.stdout.strip()) if result.ok and result.stdout.strip().isdigit() else None
    return None


def meminfo_total(text: str) -> int | None:
    """`MemTotal` from `/proc/meminfo`, in bytes."""
    for line in text.splitlines():
        name, _, value = line.partition(":")
        if name == "MemTotal":
            kib = value.split()[0] if value.split() else ""
            return int(kib) * 1024 if kib.isdigit() else None
    return None
