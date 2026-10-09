"""`~/.tangier/config.toml`, read by `home.load_config`."""

import os
import tempfile
import unittest
from unittest import mock

from tangier import home
from tangier.gate import GateError


class TestLoadConfig(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"TANGIER_HOME": tmp.name})
        _ = patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, text: str) -> None:
        with open(os.path.join(home.root(), "config.toml"), "w") as fh:
            _ = fh.write(text)

    # SPEC: gate#job-config
    def test_no_file_gives_four_running_jobs(self) -> None:
        self.assertEqual(home.load_config().max_running, 4)

    # SPEC: gate#job-config
    def test_max_running_overrides_the_default(self) -> None:
        self.write("[jobs]\nmax-running = 2\n")
        self.assertEqual(home.load_config().max_running, 2)

    # SPEC: gate#job-config
    def test_a_bad_value_is_an_error(self) -> None:
        for value in ("0", '"2"', "true", "1.5"):
            with self.subTest(value=value):
                self.write(f"[jobs]\nmax-running = {value}\n")
                with self.assertRaisesRegex(GateError, "max-running must be a whole number of 1 or more"):
                    _ = home.load_config()

    # SPEC: gate#job-config
    def test_an_unknown_key_is_an_error(self) -> None:
        for text, message in (
            ("[jobs]\nmax-runing = 2\n", r"unknown key in \[jobs\]: max-runing"),
            ("[queue]\nsize = 2\n", "unknown table or key: queue"),
            ("[jobs\n", "config.toml"),
        ):
            with self.subTest(text=text):
                self.write(text)
                with self.assertRaisesRegex(GateError, message):
                    _ = home.load_config()


class TestHome(unittest.TestCase):
    # SPEC: gate#job-home
    def test_tangier_home_moves_it(self) -> None:
        with mock.patch.dict(os.environ, {"TANGIER_HOME": "/elsewhere"}):
            self.assertEqual(home.root(), "/elsewhere")
        with mock.patch.dict(os.environ, {"HOME": "/home/me"}):
            _ = os.environ.pop("TANGIER_HOME", None)
            self.assertEqual(home.root(), "/home/me/.tangier")
            self.assertEqual(home.tilde("/home/me/.tangier/jobs/1/output.log"), "~/.tangier/jobs/1/output.log")
            self.assertEqual(home.tilde("/home/meadow/x"), "/home/meadow/x")


if __name__ == "__main__":
    unittest.main()
