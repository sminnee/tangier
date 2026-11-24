"""How CI, or a person at a laptop, reaches an environment's cluster.

Only `aws-ssm` needs code here: `open_tunnel`. The deploy action joins the
tailnet itself, and `none` uses the caller's kubeconfig.

Every external call goes through the Runner, the port probe included, so each
failure path is testable without AWS.
"""

from __future__ import annotations

import base64
import json
import os
import sys

from tangier.config import AwsSsmEnv, Config, DeployEnv
from tangier.runner import Runner

# How long the port-forward has to open, in one-second polls.
OPEN_POLLS = 60


class ConnectError(RuntimeError):
    """The connection could not be made. Exits 2."""


def resolve(cfg: Config, env: str) -> DeployEnv:
    """The deploy environment `env`, or an error listing the known ones."""
    found = cfg.deploy.envs.get(env)
    if found is None:
        known = ", ".join(sorted(cfg.deploy.envs)) or "(none configured)"
        raise ConnectError(f"no `[deploy.{env}]` in config (known: {known})")
    return found


def github_outputs(cfg: Config, env: str) -> list[str]:
    """The `$GITHUB_OUTPUT` lines the deploy action branches on."""
    deploy_env = resolve(cfg, env)
    lines = [f"connect={deploy_env.connect}"]
    if deploy_env.connect == "aws-ssm":
        settings = cfg.aws_ssm.envs[env]
        lines += [f"aws-role={settings.role}", f"aws-region={settings.region}"]
    return lines


def _log(message: str) -> None:
    print(f"tangier connect: {message}", file=sys.stderr)


def _aws(settings: AwsSsmEnv, *args: str) -> list[str]:
    return ["aws", *args, "--region", settings.region]


def port_answers(runner: Runner, port: int) -> bool:
    """Whether something listens on 127.0.0.1:`port`."""
    return runner.run(["bash", "-c", ': </dev/tcp/127.0.0.1/"$1"', "probe", str(port)]).ok


def token_argv(settings: AwsSsmEnv, caller_arn: str) -> list[str]:
    """The `aws eks get-token` argv the kubeconfig's exec user runs.

    A caller that already holds the role signs its own token. Any other caller,
    such as an SSO admin breaking glass, assumes the role for the token.
    """
    argv = [
        "aws",
        "eks",
        "get-token",
        "--cluster-name",
        settings.cluster_name,
        "--region",
        settings.region,
        "--output",
        "json",
    ]
    if not _holds_role(caller_arn, settings.role):
        argv += ["--role-arn", settings.role]
    return argv


def _holds_role(caller_arn: str, role_arn: str) -> bool:
    """Whether `caller_arn` is a session of `role_arn`: same account, same role name."""
    caller = caller_arn.split(":", 5)
    role = role_arn.split(":", 5)
    if len(caller) < 6 or len(role) < 6 or caller[4] != role[4]:
        return False
    return caller[5].startswith(f"assumed-role/{role[5].rsplit('/', 1)[-1]}/")


def kubeconfig_yaml(settings: AwsSsmEnv, ca_data: str, token: list[str], profile: str | None) -> str:
    """A kubeconfig for the tunnel's local port. Each string is JSON-quoted, which YAML reads as-is."""
    name = settings.cluster_name
    q = json.dumps
    lines = [
        "apiVersion: v1",
        "kind: Config",
        "clusters:",
        f"  - name: {q(name)}",
        "    cluster:",
        f"      server: {q(f'https://127.0.0.1:{settings.local_port}')}",
        f"      certificate-authority-data: {q(ca_data)}",
        "users:",
        f"  - name: {q(f'{name}-{settings.name}')}",
        "    user:",
        "      exec:",
        "        apiVersion: client.authentication.k8s.io/v1beta1",
        f"        command: {q(token[0])}",
        "        interactiveMode: Never",
        "        args:",
        *[f"          - {q(arg)}" for arg in token[1:]],
    ]
    if profile:
        lines += ["        env:", "          - name: AWS_PROFILE", f"            value: {q(profile)}"]
    lines += [
        "contexts:",
        f"  - name: {q(name)}",
        "    context:",
        f"      cluster: {q(name)}",
        f"      user: {q(f'{name}-{settings.name}')}",
        f"current-context: {q(name)}",
        "",
    ]
    return "\n".join(lines)


def _online_node(runner: Runner, settings: AwsSsmEnv) -> str:
    """The first running instance named `node_tag` that SSM reports Online."""
    found = runner.run(
        _aws(
            settings,
            "ec2",
            "describe-instances",
            "--filters",
            f"Name=tag:Name,Values={settings.node_tag}",
            "Name=instance-state-name,Values=running",
            "--query",
            "Reservations[].Instances[].InstanceId",
            "--output",
            "json",
        )
    )
    if not found.ok:
        raise ConnectError(f"could not list the instances named `{settings.node_tag}`:\n{found.stderr.strip()}")
    instance_ids = _json_list(found.stdout)
    if instance_ids:
        online = runner.run(
            _aws(
                settings,
                "ssm",
                "describe-instance-information",
                "--filters",
                f"Key=InstanceIds,Values={','.join(instance_ids)}",
                "Key=PingStatus,Values=Online",
                "--query",
                "InstanceInformationList[].InstanceId",
                "--output",
                "json",
            )
        )
        if not online.ok:
            raise ConnectError(f"could not ask SSM which nodes are online:\n{online.stderr.strip()}")
        online_ids = _json_list(online.stdout)
        if online_ids:
            return online_ids[0]
    raise ConnectError(f"no running instance named `{settings.node_tag}` is online in SSM")


def _json_list(stdout: str) -> list[str]:
    try:
        data = json.loads(stdout or "[]")
    except json.JSONDecodeError as e:
        raise ConnectError(f"could not parse the AWS CLI's output: {e}") from e
    return [x for x in data if isinstance(x, str)] if isinstance(data, list) else []


def _write_private(path: str, text: str) -> None:
    """Write a new file, mode 600, then move it over `path`, so a reader never sees half a file."""
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        _ = fh.write(text)
    os.replace(tmp, path)


def open_tunnel(runner: Runner, settings: AwsSsmEnv, workdir: str, *, profile: str | None = None) -> tuple[str, int]:
    """Open the SSM port-forward for `settings` and write its kubeconfig in `workdir`.

    Returns the kubeconfig's path and the session's process group. The session
    leads its own process group, so it outlives this call: in CI the runner reaps
    it at job end. Every failure raises before the session starts, or stops it.
    """
    for program in ("aws", "session-manager-plugin"):
        if runner.which(program) is None:
            raise ConnectError(
                f"{program} not found on PATH — the tunnel needs the AWS CLI v2 and its Session Manager plugin"
            )

    caller = runner.run(["aws", "sts", "get-caller-identity", "--query", "Arn", "--output", "text"])
    if not caller.ok:
        raise ConnectError(
            f"the AWS credentials do not work (for a profile, run `aws sso login`):\n{caller.stderr.strip()}"
        )
    caller_arn = caller.stdout.strip()
    token = token_argv(settings, caller_arn)
    _log(f"caller {caller_arn}; token: {' '.join(token)}")

    # kubectl reports only the exec command's exit code, so check it once here,
    # where its stderr can be shown. The token itself goes nowhere.
    checked = runner.run(token)
    if not checked.ok:
        raise ConnectError(f"the token command failed:\n{checked.stderr.strip()}")

    target = _online_node(runner, settings)

    if port_answers(runner, settings.local_port):
        raise ConnectError(
            f"127.0.0.1:{settings.local_port} is in use — stop the other tunnel, "
            f"or set `[aws-ssm.{settings.name}] local_port`"
        )

    ca = runner.run(
        _aws(
            settings,
            "ssm",
            "get-parameter",
            "--name",
            settings.ca_parameter,
            "--query",
            "Parameter.Value",
            "--output",
            "text",
        )
    )
    if not ca.ok:
        raise ConnectError(f"could not read the cluster CA from `{settings.ca_parameter}`:\n{ca.stderr.strip()}")
    ca_data = base64.b64encode(ca.stdout.strip().encode() + b"\n").decode()

    os.makedirs(workdir, exist_ok=True)
    kubeconfig = os.path.join(workdir, "kubeconfig")
    session_log = os.path.join(workdir, "session.log")
    _write_private(kubeconfig, kubeconfig_yaml(settings, ca_data, token, profile))
    # The runner appends to the log; a failure should show this session's output alone.
    with open(session_log, "w"):
        pass

    pid = runner.spawn(
        _aws(
            settings,
            "ssm",
            "start-session",
            "--target",
            target,
            "--document-name",
            "AWS-StartPortForwardingSession",
            "--parameters",
            f"portNumber={settings.remote_port},localPortNumber={settings.local_port}",
        ),
        log=session_log,
    )
    try:
        for _ in range(OPEN_POLLS):
            if port_answers(runner, settings.local_port):
                _log(f"{target}:{settings.remote_port} on 127.0.0.1:{settings.local_port}")
                return kubeconfig, pid
            if not runner.run(["kill", "-0", str(pid)]).ok:
                break
            runner.sleep(1)
        raise ConnectError(f"the port-forward did not open. Session log:\n{_read(session_log)}")
    except BaseException:
        # Ctrl-C included: the session is in its own session, so nothing else stops it.
        # The port-forward runs in session-manager-plugin, a child that ignores a
        # signal to the aws process alone, so stop the whole group.
        _ = runner.run(["kill", "--", f"-{pid}"])
        raise


def _read(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip() or "(empty)"
    except OSError:
        return "(none)"
