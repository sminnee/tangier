"""Gate run statistics: pass rates, durations, flaky keys and the tests that fail most.

A pure function of the runs, so it is tested without git. `gate stats`
gathers the runs. See `docs/specs/gate.md#stats`.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

K = TypeVar("K")


@dataclass(frozen=True)
class Run:
    """One run from a record: a pass from `refs/tangier/gates`, or a failure from `refs/tangier/failures`."""

    gate: str
    key: str
    passed: bool
    record: Mapping[str, object]


def aggregate(
    runs: Sequence[Run],
    gates: Sequence[str],
    *,
    since: datetime | None = None,
    kind: str | None = None,
    top: int = 20,
) -> dict[str, object]:
    """The statistics for `gates`, in that order, over the runs at or after `since` on a runner of `kind`.

    A run with no readable `time` is left out once `since` is set. Returns plain
    data, ready for JSON.
    """
    chosen = [run for run in runs if run.gate in gates and _within(run, since, kind)]
    return {
        "since": since.isoformat(timespec="seconds") if since else None,
        "kind": kind,
        "gates": [_gate_row(name, [run for run in chosen if run.gate == name]) for name in gates],
        "top_tests": _top_tests(chosen, top),
        "top_files": _top_files(chosen, top),
    }


def _percentile(values: Sequence[float], p: float) -> float | None:
    """The nearest-rank `p`th percentile, `p` in (0, 1]. None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


def _within(run: Run, since: datetime | None, kind: str | None) -> bool:
    runner = run.record.get("runner")
    if kind is not None and not (isinstance(runner, Mapping) and runner.get("kind") == kind):
        return False
    if since is None:
        return True
    when = _time(run)
    return when is not None and when >= since


def _time(run: Run) -> datetime | None:
    """The run's `time`. None when it is missing, unreadable, or has no UTC offset, so cannot be compared."""
    try:
        when = datetime.fromisoformat(str(run.record["time"]))
    except (KeyError, ValueError):
        return None
    return when if when.tzinfo is not None else None


def _number(value: object) -> float | None:
    """A finite number from a record, which may come from origin and be anything."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _spread(values: list[float]) -> dict[str, float | None]:
    return {"median": statistics.median(values) if values else None, "p90": _percentile(values, 0.9)}


def _gate_row(name: str, runs: list[Run]) -> dict[str, object]:
    passed = [run for run in runs if run.passed]
    failed = [run for run in runs if not run.passed]

    def durations(chosen: list[Run]) -> list[float]:
        return [d for d in (_number(run.record.get("duration")) for run in chosen) if d is not None]

    loads: list[float] = []
    for run in runs:
        load = run.record.get("load")
        if isinstance(load, Mapping):
            value, cpus = _number(load.get("load")), _number(load.get("cpus"))
            if value is not None and cpus:
                loads.append(value / cpus)
    outcomes: dict[str, set[bool]] = {}
    for run in runs:
        outcomes.setdefault(run.key, set()).add(run.passed)
    return {
        "gate": name,
        "runs": len(runs),
        "passed": len(passed),
        "failed": len(failed),
        "pass_rate": len(passed) / len(runs) if runs else None,
        "duration": {"passed": _spread(durations(passed)), "failed": _spread(durations(failed))},
        "load_per_cpu": statistics.median(loads) if loads else None,
        "flaky_keys": sum(1 for seen in outcomes.values() if len(seen) == 2),
    }


def _failures(runs: list[Run]) -> list[tuple[Run, Mapping[str, object]]]:
    """Each failing test in each failed run, with its run."""
    found = []
    for run in runs:
        entries = run.record.get("failures")
        if not run.passed and isinstance(entries, list):
            found += [(run, entry) for entry in entries if isinstance(entry, Mapping)]
    return found


class _Tally:
    """How often one test or file failed, where, how, and when last."""

    def __init__(self) -> None:
        self.count = 0
        self.gates: set[str] = set()
        self.types: set[str] = set()
        self.file: str | None = None
        self.last: str = ""

    def add(self, run: Run, entry: Mapping[str, object]) -> None:
        self.count += 1
        self.gates.add(run.gate)
        if isinstance(entry.get("type"), str):
            self.types.add(str(entry["type"]))
        when = str(run.record.get("time") or "")
        if when >= self.last:
            self.last = when
            if isinstance(entry.get("file"), str):
                self.file = str(entry["file"])

    def data(self) -> dict[str, object]:
        return {"count": self.count, "gates": sorted(self.gates), "last_seen": self.last or None}


def _ranked(tallies: Mapping[K, _Tally], top: int) -> list[tuple[K, _Tally]]:
    """The `top` most frequent, most recent first among equals."""
    by_recent = sorted(tallies.items(), key=lambda item: item[1].last, reverse=True)
    return sorted(by_recent, key=lambda item: item[1].count, reverse=True)[:top]


def _top_tests(runs: list[Run], top: int) -> list[dict[str, object]]:
    tallies: dict[tuple[str, str], _Tally] = {}
    for run, entry in _failures(runs):
        ident = (str(entry.get("classname") or ""), str(entry.get("test") or ""))
        tallies.setdefault(ident, _Tally()).add(run, entry)
    return [
        {
            "classname": classname or None,
            "test": test or None,
            "file": tally.file,
            **tally.data(),
            "types": sorted(tally.types),
        }
        for (classname, test), tally in _ranked(tallies, top)
    ]


def _top_files(runs: list[Run], top: int) -> list[dict[str, object]]:
    tallies: dict[str, _Tally] = {}
    for run, entry in _failures(runs):
        if isinstance(entry.get("file"), str):
            tallies.setdefault(str(entry["file"]), _Tally()).add(run, entry)
    return [{"file": file, **tally.data()} for file, tally in _ranked(tallies, top)]
