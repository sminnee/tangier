"""`tangier connect show` and `tangier connect open`, through the CLI with a fake runner.

The fake answers every `aws` call and the port probe, so each failure path runs
without AWS. Errors are asserted on the fragment a person acts on.
"""

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from tangier import cli
from tangier.connect import OPEN_POLLS
from tangier.runner import Result
from tangier.tests.support import RecordingRunner, write_config

ROLE = "arn:aws:iam::1:role/ci/deploy-uat"
CONFIG = f"""
[deploy]
connect = "aws-ssm"

[deploy.uat]
namespace = "u"
overlay = "o"

[deploy.linode]
namespace = "l"
overlay = "o"
connect = "tailnet"

[aws-ssm]
region = "ap-southeast-6"
node_tag = "k3s-*"
ca_parameter = "/k3s/ca"
cluster_name = "astro"

[aws-ssm.uat]
role = "{ROLE}"
local_port = 7443
"""

PROBE = ("bash", "-c", ': </dev/tcp/127.0.0.1/"$1"', "probe", "7443")


def healthy(
    overrides: dict | None = None, caller: str = "arn:aws:sts::1:assumed-role/AdminAccess/sam"
) -> RecordingRunner:
    responses = {
        ("aws", "sts", "get-caller-identity"): Result(0, stdout=caller + "\n"),
        ("aws", "eks", "get-token"): Result(0, stdout="{}"),
        ("aws", "ec2", "describe-instances"): Result(0, stdout=json.dumps(["i-a", "i-b"])),
        ("aws", "ssm", "describe-instance-information"): Result(0, stdout=json.dumps(["i-b"])),
        ("aws", "ssm", "get-parameter"): Result(0, stdout="-----BEGIN CERTIFICATE-----\n"),
        # Closed before the session starts, then open on the second poll.
        PROBE: [Result(1), Result(1), Result(0)],
    }
    responses.update(overrides or {})
    return RecordingRunner(responses, spawn_pid=4242)


def kubeconfig(role_arn: bool = True, profile: str | None = None) -> str:
    """The kubeconfig `healthy()` leads to. The CA is base64 of the PEM line plus a newline."""
    role = ['          - "--role-arn"', f'          - "{ROLE}"'] if role_arn else []
    env = ["        env:", "          - name: AWS_PROFILE", f'            value: "{profile}"'] if profile else []
    return "\n".join(
        [
            "apiVersion: v1",
            "kind: Config",
            "clusters:",
            '  - name: "astro"',
            "    cluster:",
            '      server: "https://127.0.0.1:7443"',
            '      certificate-authority-data: "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCg=="',
            "users:",
            '  - name: "astro-uat"',
            "    user:",
            "      exec:",
            "        apiVersion: client.authentication.k8s.io/v1beta1",
            '        command: "aws"',
            "        interactiveMode: Never",
            "        args:",
            '          - "eks"',
            '          - "get-token"',
            '          - "--cluster-name"',
            '          - "astro"',
            '          - "--region"',
            '          - "ap-southeast-6"',
            '          - "--output"',
            '          - "json"',
            *role,
            *env,
            "contexts:",
            '  - name: "astro"',
            "    context:",
            '      cluster: "astro"',
            '      user: "astro-uat"',
            'current-context: "astro"',
            "",
        ]
    )


class ConnectCase(unittest.TestCase):
    def setUp(self) -> None:
        self.config = write_config(self, CONFIG)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        _ = os.environ.pop("AWS_PROFILE", None)

    def run_cli(self, *argv: str, runner: RecordingRunner | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--config", self.config, "connect", *argv], runner=runner or RecordingRunner())
        return code, out.getvalue(), err.getvalue()

    def open(self, runner: RecordingRunner) -> tuple[int, str, str]:
        return self.run_cli("open", "uat", "--dir", self.dir, runner=runner)

    def written(self) -> str:
        with open(os.path.join(self.dir, "kubeconfig")) as fh:
            return fh.read()


class TestShow(ConnectCase):
    def test_aws_ssm_prints_the_method_role_and_region(self) -> None:
        code, out, _ = self.run_cli("show", "uat")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["connect=aws-ssm", f"aws-role={ROLE}", "aws-region=ap-southeast-6"])

    def test_another_method_prints_only_the_method(self) -> None:
        _, out, _ = self.run_cli("show", "linode")
        self.assertEqual(out.splitlines(), ["connect=tailnet"])

    def test_an_unknown_environment_lists_the_known_ones(self) -> None:
        code, _, err = self.run_cli("show", "staging")
        self.assertEqual(code, 2)
        self.assertIn("known: linode, uat", err)


class TestOpen(ConnectCase):
    def test_opens_the_port_forward_to_an_online_node_and_prints_the_kubeconfig(self) -> None:
        runner = healthy()
        code, out, _ = self.open(runner)
        self.assertEqual(code, 0)
        kubeconfig = os.path.join(self.dir, "kubeconfig")
        self.assertEqual(out.strip(), kubeconfig)
        self.assertEqual(
            runner.spawned,
            [
                [
                    "aws",
                    "ssm",
                    "start-session",
                    "--target",
                    "i-b",
                    "--document-name",
                    "AWS-StartPortForwardingSession",
                    "--parameters",
                    "portNumber=6443,localPortNumber=7443",
                    "--region",
                    "ap-southeast-6",
                ]
            ],
        )
        aws = runner.commands(binary="aws")
        self.assertTrue(any("describe-instances" in c and "Name=tag:Name,Values=k3s-*" in c for c in aws), aws)
        self.assertTrue(any("get-parameter" in c and "--name /k3s/ca" in c for c in aws), aws)

    def test_the_kubeconfig_is_private_and_signs_through_the_role(self) -> None:
        _ = self.open(healthy())
        path = os.path.join(self.dir, "kubeconfig")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(self.written(), kubeconfig())

    def test_a_caller_that_already_holds_the_role_signs_its_own_token(self) -> None:
        _ = self.open(healthy(caller="arn:aws:sts::1:assumed-role/deploy-uat/GitHubActions"))
        self.assertEqual(self.written(), kubeconfig(role_arn=False))

    def test_a_role_of_the_same_name_in_another_account_still_assumes_the_role(self) -> None:
        _ = self.open(healthy(caller="arn:aws:sts::2:assumed-role/deploy-uat/GitHubActions"))
        self.assertEqual(self.written(), kubeconfig())

    def test_a_profile_is_pinned_in_the_kubeconfig(self) -> None:
        os.environ["AWS_PROFILE"] = "astro-admin"
        _ = self.open(healthy())
        self.assertEqual(self.written(), kubeconfig(profile="astro-admin"))

    def test_no_online_node_names_the_node_tag(self) -> None:
        runner = healthy({("aws", "ssm", "describe-instance-information"): Result(0, stdout="[]")})
        code, _, err = self.open(runner)
        self.assertEqual(code, 2)
        self.assertIn("`k3s-*`", err)
        self.assertEqual(runner.spawned, [])

    def test_a_port_in_use_fails_before_the_session_starts(self) -> None:
        runner = healthy({PROBE: Result(0)})
        code, _, err = self.open(runner)
        self.assertEqual(code, 2)
        self.assertIn("127.0.0.1:7443 is in use", err)
        self.assertEqual(runner.spawned, [])

    def test_a_failing_token_command_prints_its_stderr(self) -> None:
        runner = healthy({("aws", "eks", "get-token"): Result(255, stderr="AccessDenied: not authorized")})
        code, _, err = self.open(runner)
        self.assertEqual(code, 2)
        self.assertIn("AccessDenied: not authorized", err)
        self.assertEqual(runner.spawned, [])

    def test_a_session_that_dies_prints_its_log_and_stops_the_group(self) -> None:
        with open(os.path.join(self.dir, "session.log"), "w") as fh:
            _ = fh.write("an earlier run's log\n")
        runner = healthy({PROBE: Result(1), ("kill", "-0"): [Result(0), Result(1)]})
        original_spawn = runner.spawn

        def spawn(argv, *, log, env=None, pass_fds=()):
            with open(log, "a") as fh:
                _ = fh.write("TargetNotConnected\n")
            return original_spawn(argv, log=log, env=env, pass_fds=pass_fds)

        runner.spawn = spawn
        code, out, err = self.open(runner)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("TargetNotConnected", err)
        self.assertNotIn("an earlier run's log", err)
        self.assertIn(["kill", "--", "-4242"], runner.calls)

    def test_a_port_that_never_opens_times_out_and_stops_the_group(self) -> None:
        runner = healthy({PROBE: Result(1)})
        code, out, err = self.open(runner)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("did not open", err)
        self.assertEqual(len(runner.slept), OPEN_POLLS)
        self.assertIn(["kill", "--", "-4242"], runner.calls)

    def test_an_interrupt_while_waiting_stops_the_group(self) -> None:
        runner = healthy({PROBE: Result(1)})

        def interrupted(seconds: float) -> None:
            raise KeyboardInterrupt

        runner.sleep = interrupted
        with self.assertRaises(KeyboardInterrupt):
            _ = self.open(runner)
        self.assertIn(["kill", "--", "-4242"], runner.calls)

    def test_an_environment_of_another_method_names_it(self) -> None:
        runner = RecordingRunner()
        code, _, err = self.run_cli("open", "linode", runner=runner)
        self.assertEqual(code, 2)
        self.assertIn("connects by tailnet", err)
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
