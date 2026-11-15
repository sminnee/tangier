"""`.github/actions/tailnet/resolve.sh`: the tag and operator it hands the connect step.

Run as a real script against a temp `pipeline.toml`, because its precedence
lives in inline Python inside bash, where nothing else would catch a slip.
"""

import os
import subprocess
import tempfile
import unittest

from tangier.tests.support import REPO_ROOT

SCRIPT = os.path.join(REPO_ROOT, ".github", "actions", "tailnet", "resolve.sh")

TWO_CLUSTERS = """
[tailnet]
operator = "ts-op"

[tailnet.uat]
tag = "tag:uat"

[tailnet.uat-aws]
tag = "tag:uat-aws"
operator = "aws-op"
"""


def resolve(pipeline_toml: str | None, env: str, operator: str = "", tag: str = "") -> dict[str, str]:
    """Run the script in a temp dir and return what it wrote to `$GITHUB_OUTPUT`."""
    with tempfile.TemporaryDirectory() as tmp:
        if pipeline_toml is not None:
            with open(os.path.join(tmp, "pipeline.toml"), "w") as fh:
                _ = fh.write(pipeline_toml)
        output = os.path.join(tmp, "github_output")
        child_env = {
            "PATH": os.environ["PATH"],
            "GITHUB_OUTPUT": output,
            "INPUT_ENV": env,
            "INPUT_TAG": tag,
            "INPUT_OPERATOR": operator,
            "INPUT_VERSION": "",
        }
        _ = subprocess.run(["bash", SCRIPT], cwd=tmp, env=child_env, check=True, capture_output=True)
        with open(output) as fh:
            return dict(line.split("=", 1) for line in fh.read().splitlines())


class TestResolveOperator(unittest.TestCase):
    def test_an_env_operator_overrides_the_shared_one(self) -> None:
        out = resolve(TWO_CLUSTERS, "uat-aws")
        self.assertEqual((out["tag"], out["operator"]), ("tag:uat-aws", "aws-op"))

    def test_an_env_without_one_falls_back_to_the_shared_operator(self) -> None:
        self.assertEqual(resolve(TWO_CLUSTERS, "uat")["operator"], "ts-op")

    def test_with_no_operator_anywhere_the_default_applies(self) -> None:
        self.assertEqual(resolve('[tailnet.uat]\ntag = "tag:uat"\n', "uat")["operator"], "tailscale-operator")

    def test_an_explicit_input_wins(self) -> None:
        self.assertEqual(resolve(TWO_CLUSTERS, "uat-aws", operator="input-op")["operator"], "input-op")

    def test_no_config_needs_only_the_tag_input(self) -> None:
        out = resolve(None, "", tag="tag:cluster")
        self.assertEqual((out["tag"], out["operator"]), ("tag:cluster", "tailscale-operator"))
