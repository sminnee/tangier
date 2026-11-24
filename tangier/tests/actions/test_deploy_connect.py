"""The deploy action's scripts, run for real with fakes on PATH.

`check-connect.sh` is the fail-fast check on each method's inputs. `deploy.sh`
hands the tunnel's kubeconfig and keys to `tangier deploy` for aws-ssm alone.
"""

import os
import subprocess
import tempfile
import unittest

from tangier.tests.support import REPO_ROOT

SCRIPT = os.path.join(REPO_ROOT, ".github", "actions", "deploy", "check-connect.sh")
DEPLOY = os.path.join(REPO_ROOT, ".github", "actions", "deploy", "deploy.sh")


def check(connect: str, client_id: str = "", audience: str = "", kubectl_exit: int | None = None):
    """Run the script. `kubectl_exit` puts a fake kubectl on PATH that exits with it."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.environ["PATH"]
        if kubectl_exit is not None:
            fake = os.path.join(tmp, "kubectl")
            with open(fake, "w") as fh:
                _ = fh.write(f"#!/bin/sh\necho ctx-from-caller\nexit {kubectl_exit}\n")
            os.chmod(fake, 0o755)
            path = f"{tmp}:{path}"
        env = {
            "PATH": path,
            "CONNECT": connect,
            "DEPLOY_ENV": "uat",
            "INPUT_CLIENT_ID": client_id,
            "INPUT_AUDIENCE": audience,
        }
        return subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True)


class TestTailnet(unittest.TestCase):
    def test_both_inputs_pass(self) -> None:
        self.assertEqual(check("tailnet", "id", "aud").returncode, 0)

    def test_an_empty_client_id_is_named(self) -> None:
        result = check("tailnet", "", "aud")
        self.assertEqual(result.returncode, 1)
        self.assertIn("`client-id` is empty", result.stderr)

    def test_an_empty_audience_is_named(self) -> None:
        result = check("tailnet", "id", "")
        self.assertEqual(result.returncode, 1)
        self.assertIn("`audience` is empty", result.stderr)


class TestNone(unittest.TestCase):
    def test_a_selected_context_passes(self) -> None:
        result = check("none", kubectl_exit=0)
        self.assertEqual(result.returncode, 0)
        self.assertIn("ctx-from-caller", result.stdout)

    def test_no_context_names_kubeconfig(self) -> None:
        result = check("none", kubectl_exit=1)
        self.assertEqual(result.returncode, 1)
        self.assertIn("KUBECONFIG", result.stderr)


class TestAwsSsm(unittest.TestCase):
    def test_needs_no_tailnet_inputs(self) -> None:
        self.assertEqual(check("aws-ssm").returncode, 0)


def deploy(**env: str) -> str:
    """Run deploy.sh with a fake `tangier` that prints its argv and the variables it sees."""
    with tempfile.TemporaryDirectory() as tmp:
        fake = os.path.join(tmp, "tangier")
        with open(fake, "w") as fh:
            _ = fh.write(
                '#!/bin/sh\necho "argv=$*"\n'
                'echo "KUBECONFIG=${KUBECONFIG-unset}"\n'
                'echo "AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID-unset}"\n'
                'echo "TUNNEL_AWS_ACCESS_KEY_ID=${TUNNEL_AWS_ACCESS_KEY_ID-unset}"\n'
            )
        os.chmod(fake, 0o755)
        child = {"PATH": f"{tmp}:{os.environ['PATH']}", "DEPLOY_ENV": "uat", **env}
        return subprocess.run(["bash", DEPLOY], env=child, capture_output=True, text=True, check=True).stdout


class TestDeploy(unittest.TestCase):
    TUNNEL = {
        "TUNNEL_KUBECONFIG": "/tmp/t/kubeconfig",
        "TUNNEL_AWS_ACCESS_KEY_ID": "AKIA",
        "TUNNEL_AWS_SECRET_ACCESS_KEY": "s",
        "TUNNEL_AWS_SESSION_TOKEN": "t",
    }

    def test_an_open_tunnel_supplies_the_kubeconfig_and_keys(self) -> None:
        out = deploy(**self.TUNNEL)
        self.assertIn("argv=deploy uat --summary", out)
        self.assertIn("KUBECONFIG=/tmp/t/kubeconfig", out)
        self.assertIn("AWS_ACCESS_KEY_ID=AKIA", out)
        self.assertIn("TUNNEL_AWS_ACCESS_KEY_ID=unset", out)

    def test_without_a_tunnel_the_callers_kubeconfig_and_keys_stand(self) -> None:
        empty = dict.fromkeys(self.TUNNEL, "")
        out = deploy(KUBECONFIG="/home/caller/kc", AWS_ACCESS_KEY_ID="caller", **empty)
        self.assertIn("KUBECONFIG=/home/caller/kc", out)
        self.assertIn("AWS_ACCESS_KEY_ID=caller", out)


if __name__ == "__main__":
    unittest.main()
