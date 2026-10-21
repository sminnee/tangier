"""`tangier.usage`: each process group's CPU and memory, from one `ps`."""

import sys
import unittest
from unittest import mock

from tangier import usage
from tangier.runner import Result
from tangier.tests.support import RecordingRunner

PS = ("ps", "-A", "-o", "pgid=,pcpu=,rss=")
SYSCTL = ("sysctl", "-n", "hw.memsize")


class TestSample(unittest.TestCase):
    # SPEC: gate#status-usage
    def test_it_sums_each_process_group(self) -> None:
        runner = RecordingRunner({PS: Result(0, "  100  50.0  1024\n  100  25.5  2048\n  200   0.0   512\n")})
        self.assertEqual(
            usage.sample(runner),
            {100: usage.Usage(75.5, 3 * 1024 * 1024, 2), 200: usage.Usage(0.0, 512 * 1024, 1)},
        )

    # SPEC: gate#status-usage
    def test_it_skips_a_line_it_cannot_read(self) -> None:
        runner = RecordingRunner({PS: Result(0, "  100  1.5  10\n garbage\n  100  x  10\n")})
        self.assertEqual(usage.sample(runner), {100: usage.Usage(1.5, 10 * 1024, 1)})

    def test_it_reads_a_comma_decimal_cpu_from_a_locale_that_writes_one(self) -> None:
        runner = RecordingRunner({PS: Result(0, "  100  1,5  10\n")})
        self.assertEqual(usage.sample(runner), {100: usage.Usage(1.5, 10 * 1024, 1)})

    # SPEC: gate#status-usage
    def test_a_failing_ps_gives_nothing(self) -> None:
        self.assertEqual(usage.sample(RecordingRunner({PS: Result(1, "  100  50.0  1024\n")})), {})

    # SPEC: gate#status-usage
    def test_a_missing_ps_gives_nothing(self) -> None:
        class Missing(RecordingRunner):
            def run(self, argv: list[str], **kwargs: object) -> Result:
                raise FileNotFoundError(argv[0])

        self.assertEqual(usage.sample(Missing()), {})


class TestMemory(unittest.TestCase):
    # SPEC: gate#status-usage
    def test_meminfo_total_is_in_bytes(self) -> None:
        text = "MemFree:  1000 kB\nMemTotal:       16384000 kB\n"
        self.assertEqual(usage.meminfo_total(text), 16384000 * 1024)

    # SPEC: gate#status-usage
    def test_meminfo_without_a_total_gives_none(self) -> None:
        self.assertIsNone(usage.meminfo_total("MemFree: 1000 kB\n"))

    # SPEC: gate#status-usage
    def test_macos_reads_the_total_from_sysctl(self) -> None:
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(usage.memory_total(RecordingRunner({SYSCTL: Result(0, "34359738368\n")})), 32 * 1024**3)
            self.assertIsNone(usage.memory_total(RecordingRunner({SYSCTL: Result(0, "unknown oid\n")})))
            self.assertIsNone(usage.memory_total(RecordingRunner({SYSCTL: Result(1, "34359738368\n")})))
