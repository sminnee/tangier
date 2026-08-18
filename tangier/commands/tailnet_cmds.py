"""`tangier tailnet ...` — preflight the path from this machine to the cluster.

A subcommand group rather than a `deploy` flag: it must run standalone, both
locally ("why can't I reach uat?") and as a CI step before anything mutates a
cluster. `deploy`'s `_dispatch` already carries four mutually-exclusive modes.

Every check produces a message naming the command that fixes it. A raw kubectl
error is exactly what this exists to replace.
"""

from __future__ import annotations

import argparse

from tangier.config import Config
from tangier.runner import Runner, Subprocess
from tangier.tailnet import TailnetError, context_matches, parse_status


def _runner(args: argparse.Namespace) -> Runner:
    return getattr(args, "runner", None) or Subprocess()


def cmd_check(config: Config, args: argparse.Namespace) -> int:
    """Run the checks in order, stopping at the first failure.

    Ordered cheapest-and-most-fundamental first, so the message a user sees
    names the earliest broken link rather than a symptom further down.
    """
    runner = _runner(args)
    env_name = getattr(args, "env", None)

    # 1. Is tailscale installed at all?
    if runner.which("tailscale") is None:
        raise TailnetError("tailscale not found on PATH — install Tailscale, then run `tailscale up`")

    # 2. Is it up?
    status_result = runner.run(["tailscale", "status", "--json"])
    if not status_result.ok:
        raise TailnetError(f"`tailscale status --json` failed (exit {status_result.returncode}) — run `tailscale up`")
    status = parse_status(status_result.stdout)
    if not status.running:
        state = status.backend_state or "unknown"
        raise TailnetError(f"tailscale is not connected (state: {state}) — run `tailscale up`")
    print(f"tailscale: connected ({len(status.tags)} tag(s))")

    # 3. Is kubectl installed?
    if runner.which("kubectl") is None:
        raise TailnetError("kubectl not found on PATH")

    # 4. Is a context selected? An unset kubeconfig prints nothing and exits
    #    non-zero, which on its own reads as a kubectl failure.
    ctx_result = runner.run(["kubectl", "config", "current-context"])
    context = ctx_result.stdout.strip()
    operator = config.tailnet.operator
    if not context:
        raise TailnetError(f"no current kubectl context — run `tailscale configure kubeconfig {operator}`")

    # 5. Is that context the operator's? Substring, because a real context is
    #    named `<cluster>@tailscale-operator` as often as it is bare.
    if not context_matches(context, operator):
        raise TailnetError(
            f"kubectl context `{context}` is not the tailnet operator `{operator}` — "
            f"run `tailscale configure kubeconfig {operator}`"
        )
    print(f"kubectl: context {context}")

    # 6. Does the API server actually answer? Everything above can be correct
    #    while the operator is unreachable.
    version = runner.run(["kubectl", "version", "--request-timeout=5s"])
    if not version.ok:
        raise TailnetError(
            f"cannot reach the cluster API server (exit {version.returncode}) — "
            f"check that the tailnet ACL grants this node access to `{operator}`"
        )
    print("cluster: reachable")

    # 7. Does this node carry the tag the environment's RBAC keys off? Only
    #    checkable when an env was named — and the one check that turns a
    #    `Forbidden` into a sentence you can act on.
    if env_name:
        tailnet_env = config.tailnet.envs.get(env_name)
        if tailnet_env is None:
            known = ", ".join(sorted(config.tailnet.envs)) or "(none configured)"
            raise TailnetError(f"no `[tailnet.{env_name}]` in config (known: {known})")
        if tailnet_env.tag not in status.tags:
            have = ", ".join(sorted(status.tags)) or "(none)"
            raise TailnetError(
                f"this node is not tagged `{tailnet_env.tag}`, which {env_name} requires "
                f"(node tags: {have}) — deploys to {env_name} will be rejected by RBAC"
            )
        print(f"tailnet tag: {tailnet_env.tag}")

    return 0


def add_parsers(sub: argparse._SubParsersAction) -> None:
    tn = sub.add_parser("tailnet", help="check the path from here to the cluster over the tailnet")
    tnsub = tn.add_subparsers(dest="cmd", required=True)

    cp = tnsub.add_parser("check", help="verify tailscale, kubectl, the operator context, and the env's tag")
    # Optional, so a repo with no `[tailnet.<env>]` at all still gets checks 1-6.
    _ = cp.add_argument("env", nargs="?", help="environment name from [tailnet.<env>]")
    # The only command that runs without a config: the connectivity checks need
    # nothing from it, and the repos most likely to want them have no images.
    cp.set_defaults(func=cmd_check, config_optional=True)
