"""`tangier.stats` and `gate stats`: aggregation as a pure function, then the command over seeded records."""

import json
import subprocess
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from unittest import mock

from tangier import gate, stats
from tangier.tests.support import make_origin
from tangier.tests.test_gate import GateCase, _put_blob

LOCAL = {"kind": "local", "host": "dev"}
CI = {"kind": "ci", "provider": "github-actions"}


def _run(
    name: str,
    key: str,
    passed: bool,
    day: int,
    duration: float = 10.0,
    runner: dict[str, str] = LOCAL,
    failures: list[Mapping[str, object]] | None = None,
    load: dict[str, float] | None = None,
) -> stats.Run:
    record: dict[str, object] = {"time": f"2026-03-{day:02d}T00:00:00+00:00", "duration": duration, "runner": runner}
    if failures is not None:
        record["failures"] = failures
    if load is not None:
        record["load"] = load
    return stats.Run(name, key, passed, record)


BAD_PASSWORD = {"classname": "tests.test_api.TestLogin", "test": "test_bad_password", "file": "tests/test_api.py"}


class TestAggregate(unittest.TestCase):
    # SPEC: gate#stats
    def test_counts_rates_and_duration_spreads_per_gate(self) -> None:
        runs = [_run("test", f"k{i}", True, 1, duration=float(i)) for i in range(1, 11)]
        runs += [_run("test", "f1", False, 2, duration=3.0), _run("lint", "l1", True, 1, duration=0.5)]
        data = stats.aggregate(runs, ["lint", "test", "e2e"])
        self.assertEqual([row["gate"] for row in data["gates"]], ["lint", "test", "e2e"])
        _, test, e2e = data["gates"]
        self.assertAlmostEqual(test.pop("pass_rate"), 10 / 11)
        # Nearest rank: p90 of 1..10 is 9, where interpolating would give 9.1.
        self.assertEqual(
            test,
            {
                "gate": "test",
                "runs": 11,
                "passed": 10,
                "failed": 1,
                "duration": {"passed": {"median": 5.5, "p90": 9.0}, "failed": {"median": 3.0, "p90": 3.0}},
                "load_per_cpu": None,
                "flaky_keys": 0,
            },
        )
        self.assertEqual(
            e2e,
            {
                "gate": "e2e",
                "runs": 0,
                "passed": 0,
                "failed": 0,
                "pass_rate": None,
                "duration": {"passed": {"median": None, "p90": None}, "failed": {"median": None, "p90": None}},
                "load_per_cpu": None,
                "flaky_keys": 0,
            },
        )

    # SPEC: gate#stats
    def test_load_is_the_median_per_cpu(self) -> None:
        runs = [
            _run("test", "a", True, 1, load={"load": 4.0, "cpus": 8}),
            _run("test", "b", True, 1, load={"load": 16.0, "cpus": 8}),
            _run("test", "c", True, 1, load={"load": 8.0, "cpus": 8}),
            _run("test", "d", True, 1),
        ]
        self.assertEqual(stats.aggregate(runs, ["test"])["gates"][0]["load_per_cpu"], 1.0)

    # SPEC: gate#stats-flaky
    def test_a_key_with_both_outcomes_is_flaky(self) -> None:
        runs = [
            _run("test", "same", True, 1),
            _run("test", "same", False, 2),
            _run("test", "only-pass", True, 1),
            _run("test", "only-fail", False, 1),
        ]
        self.assertEqual(stats.aggregate(runs, ["test"])["gates"][0]["flaky_keys"], 1)

    # SPEC: gate#stats-filters
    def test_a_time_with_no_offset_is_left_out_once_since_is_set(self) -> None:
        runs = [stats.Run("test", "a", True, {"time": "2026-03-06", "runner": LOCAL}), _run("test", "b", True, 6)]
        since = datetime(2026, 3, 5, tzinfo=UTC)
        self.assertEqual(stats.aggregate(runs, ["test"], since=since)["gates"][0]["runs"], 1)

    # SPEC: gate#stats-filters
    def test_since_and_kind_filter_runs(self) -> None:
        runs = [
            _run("test", "a", True, 1),
            _run("test", "b", True, 5, runner=CI),
            _run("test", "c", False, 6),
            stats.Run("test", "d", True, {"runner": LOCAL}),
        ]
        since = datetime(2026, 3, 5, tzinfo=UTC)
        self.assertEqual(stats.aggregate(runs, ["test"])["gates"][0]["runs"], 4)
        self.assertEqual(stats.aggregate(runs, ["test"], since=since)["gates"][0]["runs"], 2)
        ci = stats.aggregate(runs, ["test"], since=since, kind="ci")
        self.assertEqual((ci["gates"][0]["runs"], ci["kind"], ci["since"]), (1, "ci", "2026-03-05T00:00:00+00:00"))

    # SPEC: gate#stats-top-failures
    def test_top_tests_and_files_rank_by_count_across_gates(self) -> None:
        lint = {"classname": "src/app.py", "test": "org.ruff.F401", "file": "src/app.py", "type": "F401"}
        runs = [
            _run("test.py311", "a", False, 1, failures=[{**BAD_PASSWORD, "type": "AssertionError"}]),
            _run("test.py312", "b", False, 3, failures=[{**BAD_PASSWORD, "type": "TimeoutError"}]),
            _run("lint", "c", False, 2, failures=[lint]),
            # A pass's entries, if any, are not failures.
            _run("test.py311", "d", True, 4, failures=[lint, lint]),
        ]
        data = stats.aggregate(runs, ["lint", "test.py311", "test.py312"], top=5)
        self.assertEqual(
            data["top_tests"],
            [
                {
                    "classname": "tests.test_api.TestLogin",
                    "test": "test_bad_password",
                    "file": "tests/test_api.py",
                    "count": 2,
                    "gates": ["test.py311", "test.py312"],
                    "last_seen": "2026-03-03T00:00:00+00:00",
                    "types": ["AssertionError", "TimeoutError"],
                },
                {
                    "classname": "src/app.py",
                    "test": "org.ruff.F401",
                    "file": "src/app.py",
                    "count": 1,
                    "gates": ["lint"],
                    "last_seen": "2026-03-02T00:00:00+00:00",
                    "types": ["F401"],
                },
            ],
        )
        self.assertEqual(
            [(f["file"], f["count"]) for f in data["top_files"]], [("tests/test_api.py", 2), ("src/app.py", 1)]
        )
        self.assertEqual(len(stats.aggregate(runs, ["lint", "test.py311", "test.py312"], top=1)["top_tests"]), 1)

    # SPEC: gate#stats-top-failures
    def test_ties_go_to_the_most_recent_and_the_newest_file_shows(self) -> None:
        moved = {"classname": "c", "test": "moved"}
        runs = [
            _run("test", "a", False, 1, failures=[{"classname": "c", "test": "older"}]),
            _run("test", "b", False, 3, failures=[{**moved, "file": "new/place.py"}]),
            _run("test", "c", False, 2, failures=[{**moved, "file": "old/place.py"}]),
            _run("test", "d", False, 4, failures=[{"classname": "c", "test": "newer"}]),
            _run("test", "e", False, 2, failures=[{"classname": "c", "test": "older"}]),
        ]
        data = stats.aggregate(runs, ["test"])
        self.assertEqual(
            [(t["test"], t["count"], t["file"]) for t in data["top_tests"]],
            [("moved", 2, "new/place.py"), ("older", 2, None), ("newer", 1, None)],
        )


class TestStatsCommand(GateCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(gate, "now", return_value=datetime(2026, 3, 10, tzinfo=UTC))
        _ = patcher.start()
        self.addCleanup(patcher.stop)
        runs = [
            {"time": "2026-03-08T00:00:00+00:00", "duration": 12.0, "runner": LOCAL},
            {"time": "2026-03-09T00:00:00+00:00", "duration": 14.0, "runner": CI},
            # Outside the default 30 days.
            {"time": "2026-01-01T00:00:00+00:00", "duration": 99.0, "runner": LOCAL},
        ]
        failure = {
            "time": "2026-03-09T12:00:00+00:00",
            "duration": 5.0,
            "runner": LOCAL,
            "code": 1,
            "failures": [{**BAD_PASSWORD, "outcome": "failure", "type": "AssertionError"}],
        }
        record = {"format": 2, "gate": "backend", "key": "k1", "runs": runs}
        _put_blob(self.repo, "refs/tangier/gates/backend/k1", json.dumps(record))
        _put_blob(self.repo, "refs/tangier/failures/backend/k1", json.dumps({**record, "runs": [failure]}))
        # A gate the config no longer has.
        old = {"format": 2, "gate": "retired", "key": "k9", "runs": [runs[0]]}
        _put_blob(self.repo, "refs/tangier/gates/retired/k9", json.dumps(old))

    # SPEC: gate#stats
    # SPEC: gate#stats-flaky
    # SPEC: gate#stats-top-failures
    # SPEC: gate#stats-output
    def test_the_table_lists_every_gate_then_the_top_failures(self) -> None:
        code, out, err = self.tangier("gate", "stats", "--no-fetch")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "gate runs in the last 30d (all runs)")
        self.assertEqual(
            lines[2].split(),
            ["gate", "runs", "pass", "fail", "rate", "pass", "p50/p90", "fail", "p50/p90", "load/cpu", "flaky"],
        )
        self.assertEqual(
            lines[3].split(), ["backend", "3", "2", "1", "67%", "13.0s", "/", "14.0s", "5.0s", "/", "5.0s", "-", "1"]
        )
        self.assertEqual(lines[4].split()[:4], ["retired", "1", "1", "0"])
        self.assertIn("top failing tests", lines)
        self.assertIn(
            "   1  tests.test_api.TestLogin.test_bad_password  (tests/test_api.py)  backend  AssertionError  last 12h ago",
            lines,
        )
        self.assertIn("   1  tests/test_api.py  backend  last 12h ago", lines)
        self.assertIn("newest 20 runs per key", out)

    # SPEC: gate#stats-filters
    # SPEC: gate#stats-output
    def test_json_with_a_selector_and_filters(self) -> None:
        code, out, err = self.tangier("gate", "stats", "backend", "--no-fetch", "--ci", "--since", "90d", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual([row["gate"] for row in data["gates"]], ["backend"])
        self.assertEqual((data["gates"][0]["runs"], data["kind"]), (1, "ci"))
        self.assertEqual(data["top_tests"], [])

    # SPEC: gate#stats
    def test_it_reads_origins_records_too(self) -> None:
        origin = make_origin(self, self.repo)
        other = self.clone_origin(origin)
        _ = self.tangier("gate", "sync")
        _, out, err = self.tangier("gate", "stats", "--json", cwd=other)
        data = json.loads(out)
        self.assertEqual(data["gates"][0]["runs"], 3, err)

    # SPEC: gate#stats-filters
    def test_local_and_top_narrow_the_report(self) -> None:
        _, out, _ = self.tangier("gate", "stats", "backend", "--no-fetch", "--local", "--top", "0", "--json")
        data = json.loads(out)
        self.assertEqual((data["gates"][0]["runs"], data["gates"][0]["failed"]), (2, 1))
        self.assertEqual((data["top_tests"], data["top_files"]), ([], []))

    # SPEC: gate#stats-filters
    def test_an_unreachable_origin_warns_and_local_records_still_count(self) -> None:
        code, out, err = self.tangier("gate", "stats", "backend", "--json")
        self.assertEqual(code, 0)
        self.assertIn("warning: cannot read gate records from origin", err)
        self.assertEqual(json.loads(out)["gates"][0]["runs"], 3)

    # SPEC: gate#stats-filters
    def test_no_fetch_reads_the_mirror_the_last_fetch_left(self) -> None:
        origin = make_origin(self, self.repo)
        _ = self.tangier("gate", "sync")
        other = self.clone_origin(origin)
        self.assertEqual(self.tangier("gate", "stats", "--json", cwd=other)[0], 0)
        # Origin loses its records, but this clone does not fetch again.
        _ = subprocess.run(["git", "-C", origin, "update-ref", "-d", "refs/tangier/gates/backend/k1"], check=True)
        _, out, _ = self.tangier("gate", "stats", "backend", "--no-fetch", "--json", cwd=other)
        self.assertEqual(json.loads(out)["gates"][0]["runs"], 3)
