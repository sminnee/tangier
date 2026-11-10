"""`limits`: the per-job CPU and memory limits."""

import unittest

from tangier import limits
from tangier.gate import GateError
from tangier.runner import Result
from tangier.tests.support import RecordingRunner

G = 1024**3


class TestCpuSet(unittest.TestCase):
    # SPEC: gate#job-limits
    def test_each_slot_takes_its_own_cpus(self) -> None:
        allowed = list(range(24))
        self.assertEqual(limits.cpu_set(0, 4, allowed), {0, 1, 2, 3})
        self.assertEqual(limits.cpu_set(1, 4, allowed), {4, 5, 6, 7})
        self.assertEqual(limits.cpu_set(3, 4, allowed), {12, 13, 14, 15})

    # SPEC: gate#job-limits
    def test_slots_past_the_end_wrap(self) -> None:
        allowed = list(range(10))
        self.assertEqual(limits.cpu_set(2, 4, allowed), {8, 9, 0, 1})
        self.assertEqual(limits.cpu_set(5, 4, allowed), {0, 1, 2, 3})

    # SPEC: gate#job-limits
    def test_as_many_cpus_as_allowed_or_more_pins_nothing(self) -> None:
        self.assertIsNone(limits.cpu_set(0, 4, [0, 1, 2, 3]))
        self.assertIsNone(limits.cpu_set(1, 8, [0, 1, 2, 3]))

    # SPEC: gate#job-limits
    def test_the_cpus_come_from_the_allowed_set(self) -> None:
        self.assertEqual(limits.cpu_set(1, 2, [2, 5, 7, 9, 11]), {7, 9})


class TestMemory(unittest.TestCase):
    # SPEC: gate#job-config
    def test_sizes_parse_in_powers_of_1024(self) -> None:
        for value, size in (
            ("8G", 8 * G),
            ("512m", 512 * 1024**2),
            ("2048K", 2 * 1024**2),
            ("1T", 1024 * G),
            (" 1G ", G),
            (str(G), G),
            (G, G),
        ):
            with self.subTest(value=value):
                self.assertEqual(limits.parse_memory(value), size)

    # SPEC: gate#job-config
    def test_a_bad_size_is_an_error(self) -> None:
        for value in ("8GB", "1.5G", "G", "", "-1G", True, 1.5, ["8G"], "512K", 1000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _ = limits.parse_memory(value)

    def test_show_gives_the_largest_whole_unit(self) -> None:
        self.assertEqual(limits.show(8 * G), "8G")
        self.assertEqual(limits.show(1536 * 1024**2), "1536M")
        self.assertEqual(limits.show(1024**2 + 1), "1048577")


class TestWrap(unittest.TestCase):
    # SPEC: gate#job-limits
    def test_a_memory_cap_runs_the_command_in_a_systemd_scope(self) -> None:
        self.assertEqual(
            limits.wrap(["python3", "-c", "x"], 8 * G),
            [
                "systemd-run",
                "--user",
                "--scope",
                "--quiet",
                "--collect",
                "-p",
                f"MemoryMax={8 * G}",
                "-p",
                "MemorySwapMax=0",
                "-p",
                "OOMPolicy=continue",
                "--",
                "python3",
                "-c",
                "x",
            ],
        )

    def test_no_cap_leaves_the_command_alone(self) -> None:
        self.assertEqual(limits.wrap(["python3"], None), ["python3"])


class TestCheck(unittest.TestCase):
    # SPEC: gate#job-limits
    def test_a_scope_that_starts_passes(self) -> None:
        runner = RecordingRunner()
        limits.check(runner, G)
        self.assertEqual(runner.calls, [limits.wrap(["true"], G)])

    # SPEC: gate#job-limits
    def test_a_scope_that_cannot_start_is_an_error_that_names_the_fix(self) -> None:
        runner = RecordingRunner({("systemd-run",): Result(1, stderr="Failed to connect to bus\n")})
        with self.assertRaisesRegex(GateError, "Failed to connect to bus.*systemd user session.*remove `memory`"):
            limits.check(runner, G)

    # SPEC: gate#job-limits
    def test_no_systemd_run_is_an_error(self) -> None:
        runner = RecordingRunner()
        runner.missing.add("systemd-run")
        with self.assertRaisesRegex(GateError, "systemd-run is not installed"):
            limits.check(runner, G)
        self.assertEqual(runner.calls, [])


class TestMemoryEvents(unittest.TestCase):
    def test_reads_the_oom_and_oom_kill_counts(self) -> None:
        text = "low 0\nhigh 0\nmax 37\noom 1\noom_kill 3\noom_group_kill 0\n"
        self.assertEqual(limits.parse_memory_events(text), (1, 3))

    # SPEC: gate#job-limits
    def test_only_a_kill_at_the_cgroups_own_cap_counts(self) -> None:
        self.assertTrue(limits.killed_by_cap((1, 3), (2, 4)))
        # The machine-wide OOM killer raises `oom_kill` alone.
        self.assertFalse(limits.killed_by_cap((1, 3), (1, 4)))
        self.assertFalse(limits.killed_by_cap((1, 3), (1, 3)))
        self.assertFalse(limits.killed_by_cap(None, (2, 4)))

    def test_the_cgroup_dir_comes_from_the_v2_entry(self) -> None:
        text = "1:name=systemd:/old\n0::/user.slice/user-1000.slice/app.slice/run-p1-i2.scope\n"
        self.assertEqual(limits.cgroup_dir(text), "/sys/fs/cgroup/user.slice/user-1000.slice/app.slice/run-p1-i2.scope")
        self.assertEqual(limits.cgroup_dir("0::/\n"), "/sys/fs/cgroup/")
        self.assertIsNone(limits.cgroup_dir("1:name=systemd:/old\n"))

    def test_no_count_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            _ = limits.parse_memory_events("low 0\noom_kill 3\n")


if __name__ == "__main__":
    unittest.main()
