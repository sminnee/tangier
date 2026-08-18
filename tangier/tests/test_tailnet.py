"""Tailnet preflight: status parsing, context matching, and the ordered checks.

Diagnostics are asserted on the ACTIONABLE fragment — the command the user must
run, or the tag they are missing — never on whole sentences, so the wording
stays free to improve.
"""

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest

from tangier import cli
from tangier.commands import tailnet_cmds
from tangier.runner import Result
from tangier.tailnet import TailnetError, context_matches, parse_status
from tangier.tests.support import RecordingRunner, make_config

RUNNING = json.dumps({"BackendState": "Running", "Self": {"Tags": ["tag:astronort-uat-deploy"]}})


class TestParseStatus(unittest.TestCase):
    def test_running_with_tags(self) -> None:
        status = parse_status(RUNNING)
        self.assertTrue(status.running)
        self.assertEqual(status.tags, ["tag:astronort-uat-deploy"])

    def test_stopped_is_not_running(self) -> None:
        self.assertFalse(parse_status(json.dumps({"BackendState": "Stopped"})).running)

    def test_untagged_node_reports_no_tags(self) -> None:
        # `Self.Tags` is absent rather than empty on an untagged node, which is
        # the common failure this whole feature exists to diagnose.
        status = parse_status(json.dumps({"BackendState": "Running", "Self": {}}))
        self.assertEqual(status.tags, [])

    def test_malformed_json_raises_tailnet_error(self) -> None:
        with self.assertRaises(TailnetError):
            _ = parse_status("not json")

    def test_a_tag_is_matched_exactly_not_as_a_substring(self) -> None:
        # The reason for parsing rather than grepping: a hostname containing the
        # tag text must not satisfy the tag check.
        status = parse_status(json.dumps({"BackendState": "Running", "Self": {"HostName": "tag:x-deploy-box"}}))
        self.assertNotIn("tag:x-deploy", status.tags)


class TestContextMatches(unittest.TestCase):
    def test_bare_operator_name(self) -> None:
        self.assertTrue(context_matches("tailscale-operator", "tailscale-operator"))

    def test_qualified_context_name(self) -> None:
        self.assertTrue(context_matches("astronort-uat@tailscale-operator", "tailscale-operator"))

    def test_unrelated_context(self) -> None:
        self.assertFalse(context_matches("docker-desktop", "tailscale-operator"))


class TestCheck(unittest.TestCase):
    def _cfg(self, **extra):
        tables = {"tailnet.uat": {"tag": "tag:astronort-uat-deploy"}}
        tables.update(extra)
        return make_config(**tables)

    def _healthy(self) -> RecordingRunner:
        return RecordingRunner(
            {
                ("tailscale", "status"): Result(0, stdout=RUNNING),
                ("kubectl", "config", "current-context"): Result(0, stdout="astronort-uat@tailscale-operator\n"),
                ("kubectl", "version"): Result(0),
            }
        )

    def _check(self, runner, env=None, cfg=None) -> int:
        args = argparse.Namespace(env=env, runner=runner)
        with contextlib.redirect_stdout(io.StringIO()):
            return tailnet_cmds.cmd_check(cfg or self._cfg(), args)

    def _error(self, runner, env=None, cfg=None) -> str:
        with self.assertRaises(TailnetError) as ctx:
            _ = self._check(runner, env=env, cfg=cfg)
        return str(ctx.exception)

    def test_healthy_path_passes_with_an_env(self) -> None:
        self.assertEqual(self._check(self._healthy(), env="uat"), 0)

    def test_healthy_path_passes_without_an_env(self) -> None:
        # A repo with no `[tailnet.<env>]` still gets checks 1-6.
        self.assertEqual(self._check(self._healthy()), 0)

    def test_missing_tailscale_names_the_fix(self) -> None:
        runner = self._healthy()
        runner.missing.add("tailscale")
        self.assertIn("tailscale up", self._error(runner))

    def test_tailscale_down_names_the_fix(self) -> None:
        runner = self._healthy()
        runner.responses[("tailscale", "status")] = Result(0, stdout=json.dumps({"BackendState": "Stopped"}))
        self.assertIn("tailscale up", self._error(runner))

    def test_missing_kubectl_is_reported_before_any_context_lookup(self) -> None:
        runner = self._healthy()
        runner.missing.add("kubectl")
        self.assertIn("kubectl not found", self._error(runner))
        self.assertEqual(runner.commands(binary="kubectl"), [])

    def test_empty_context_names_the_configure_command(self) -> None:
        runner = self._healthy()
        runner.responses[("kubectl", "config", "current-context")] = Result(1, stdout="")
        self.assertIn("tailscale configure kubeconfig tailscale-operator", self._error(runner))

    def test_wrong_context_names_the_configure_command(self) -> None:
        runner = self._healthy()
        runner.responses[("kubectl", "config", "current-context")] = Result(0, stdout="docker-desktop\n")
        message = self._error(runner)
        self.assertIn("docker-desktop", message)
        self.assertIn("tailscale configure kubeconfig tailscale-operator", message)

    def test_unreachable_cluster_is_reported_as_such(self) -> None:
        runner = self._healthy()
        runner.responses[("kubectl", "version")] = Result(1)
        self.assertIn("cannot reach the cluster API server", self._error(runner))

    def test_missing_tag_names_the_tag(self) -> None:
        # The check the feature is for: a `Forbidden` becomes a sentence naming
        # the tag this node does not carry.
        runner = self._healthy()
        runner.responses[("tailscale", "status")] = Result(
            0, stdout=json.dumps({"BackendState": "Running", "Self": {"Tags": ["tag:something-else"]}})
        )
        message = self._error(runner, env="uat")
        self.assertIn("tag:astronort-uat-deploy", message)
        self.assertIn("tag:something-else", message)

    def test_tag_is_not_checked_without_an_env(self) -> None:
        runner = self._healthy()
        runner.responses[("tailscale", "status")] = Result(
            0, stdout=json.dumps({"BackendState": "Running", "Self": {"Tags": []}})
        )
        self.assertEqual(self._check(runner), 0)

    def test_unknown_env_lists_the_known_ones(self) -> None:
        message = self._error(self._healthy(), env="staging")
        self.assertIn("[tailnet.staging]", message)
        self.assertIn("uat", message)

    def test_a_custom_operator_appears_in_the_fix(self) -> None:
        cfg = make_config(**{"tailnet": {"operator": "ts-op"}, "tailnet.uat": {"tag": "tag:x"}})
        runner = self._healthy()
        runner.responses[("kubectl", "config", "current-context")] = Result(0, stdout="docker-desktop\n")
        self.assertIn("tailscale configure kubeconfig ts-op", self._error(runner, cfg=cfg))


class TestCheckWithoutAConfig(unittest.TestCase):
    """`tailnet check` is the one command that runs with no `pipeline.toml`.

    k8s-cluster is helm and kubectl with no images, so it carries no config —
    and it is exactly the repo most likely to need the connectivity checks.
    """

    def _run(self, argv: list[str]) -> tuple[int, str]:
        """Run the CLI in a directory with no `pipeline.toml`, returning (rc, stderr)."""
        cwd = os.getcwd()
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    rc = cli.main(argv)
            finally:
                os.chdir(cwd)
        return rc, err.getvalue()

    def test_check_gets_past_config_loading(self) -> None:
        # Both paths exit 2, so the exit code proves nothing — the message is
        # what distinguishes "no config file" from "reached the real checks".
        _rc, stderr = self._run(["tailnet", "check"])
        self.assertNotIn("config error", stderr)
        self.assertNotIn("pipeline.toml", stderr)

    def test_every_other_command_still_requires_a_config(self) -> None:
        for argv in (["changemap", "list"], ["image", "tag", "svc"], ["deploy", "uat"]):
            with self.subTest(argv=argv):
                rc, stderr = self._run(argv)
                self.assertEqual(rc, 2)
                self.assertIn("config error", stderr)


if __name__ == "__main__":
    unittest.main()
