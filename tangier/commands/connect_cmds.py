"""`tangier connect ...` — how this machine reaches an environment's cluster.

`show` tells the deploy action which method `[deploy.<env>] connect` names.
`open` opens an `aws-ssm` environment's tunnel, for that action and for a
person at a laptop breaking glass.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

from tangier.config import Config
from tangier.connect import ConnectError, github_outputs, open_tunnel, resolve
from tangier.runner import Runner, Subprocess


def _runner(args: argparse.Namespace) -> Runner:
    return getattr(args, "runner", None) or Subprocess()


def cmd_show(config: Config, args: argparse.Namespace) -> int:
    for line in github_outputs(config, args.env):
        print(line)
    return 0


def _workdir(env: str) -> str:
    """A new directory under `$RUNNER_TEMP` in CI. Locally, one fixed place per environment."""
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp:
        return tempfile.mkdtemp(prefix=f"tangier-connect-{env}.", dir=runner_temp)
    return os.path.join(os.path.expanduser("~"), ".kube", "tangier", env)


def cmd_open(config: Config, args: argparse.Namespace) -> int:
    deploy_env = resolve(config, args.env)
    if deploy_env.connect != "aws-ssm":
        raise ConnectError(f"`{args.env}` connects by {deploy_env.connect}, not aws-ssm — there is no tunnel to open")
    settings = config.aws_ssm.envs[args.env]
    workdir = args.dir or _workdir(args.env)
    kubeconfig, pid = open_tunnel(_runner(args), settings, workdir, profile=os.environ.get("AWS_PROFILE"))
    print(f"tangier connect: the tunnel runs as process group {pid}; stop it with `kill -- -{pid}`", file=sys.stderr)
    print(kubeconfig)
    return 0


def add_parsers(sub: argparse._SubParsersAction) -> None:
    cn = sub.add_parser("connect", help="reach an environment's cluster by its configured method")
    cnsub = cn.add_subparsers(dest="cmd", required=True)

    sp = cnsub.add_parser("show", help="print the environment's connection method as $GITHUB_OUTPUT lines")
    _ = sp.add_argument("env", help="environment name from [deploy.<env>]")
    sp.set_defaults(func=cmd_show)

    op = cnsub.add_parser(
        "open",
        help="open an aws-ssm environment's tunnel, then print the path of a kubeconfig that uses it",
    )
    _ = op.add_argument("env", help="environment name from [deploy.<env>]")
    _ = op.add_argument(
        "--dir",
        help="where to write the kubeconfig and session log (default: a new dir under $RUNNER_TEMP, "
        "else ~/.kube/tangier/<env>)",
    )
    op.set_defaults(func=cmd_open)
