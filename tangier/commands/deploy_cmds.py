"""`tangier deploy ...` — render, apply in two passes, wait, and roll back on failure.

Exit codes: 0 rolled out; 1 migration or rollout failure (rollback ran);
2 unknown env, bad config, or missing kubectl.
"""

from __future__ import annotations

import argparse
import os
import sys

from tangier.config import Config
from tangier.deploy import (
    DeployError,
    deployed_versions,
    deployment_targets,
    envsubst_allowlist,
    find_crashing_pods,
    migration_job_name,
    prior_version,
    render_argv,
    resolve_env,
    versions,
)
from tangier.runner import Runner, Subprocess


def _runner(args: argparse.Namespace) -> Runner:
    return getattr(args, "runner", None) or Subprocess()


def _render(runner: Runner, cfg: Config, env, version_vars: dict[str, str]) -> str:
    """Render the overlay once, with image tags substituted.

    Rendered ONCE and held in memory: that is what makes pass 2 byte-identical
    to pass 1, so the Job re-apply is a genuine no-op. It also drops the
    tempfile and its three cleanup paths.

    An empty render is rejected. `pipe` reports the LAST stage's returncode
    (shell semantics without `pipefail`), so a failing `kubectl kustomize` still
    exits 0 through `envsubst` — and empty manifests would then be applied,
    ignored per the no-`set -e` rule, and reported as a successful deploy.
    """
    allowlist = envsubst_allowlist(cfg)
    if not allowlist:
        # envsubst with an empty SHELL-FORMAT substitutes nothing, so every
        # ${VAR} would reach kubectl literally.
        raise DeployError("no SHA buckets in config: nothing to substitute into the manifests")
    child_env = {**os.environ, **version_vars}
    result = runner.pipe(render_argv(env, allowlist), env=child_env)
    if not result.ok:
        raise DeployError(f"failed to render {env.overlay}: {result.stderr.strip()}")
    if not result.stdout.strip():
        raise DeployError(f"rendering {env.overlay} produced no manifests")
    return result.stdout


def cmd_render(config: Config, args: argparse.Namespace) -> int:
    """Print the manifests a deploy would apply — how a human debugs an allowlist miss."""
    env = resolve_env(config, args.env)
    runner = _runner(args)
    print(_render(runner, config, env, versions(config, args.head)), end="")
    return 0


def cmd_versions(config: Config, args: argparse.Namespace) -> int:
    for key, value in versions(config, args.head).items():
        print(f"{key}={value}")
    return 0


def cmd_compare_env(config: Config, args: argparse.Namespace) -> int:
    """The markdown build table with an `old -> **new**` column for changed buckets.

    Relocated here from changemap: it is the only kubectl dependency, and a
    tag/path tool shelling out to a cluster is the layering violation this
    extraction exists to fix.
    """
    env = resolve_env(config, args.compare_env)
    runner = _runner(args)
    current = {b: v for b, v in _bucket_versions(config, args.head).items() if not b.endswith("-base")}
    deployed = deployed_versions(runner, config, env.namespace)
    print("## Package builds\n")
    print("| Package | Build ID |")
    print("|-----|-------|")
    for bucket, version in current.items():
        if bucket in deployed and deployed[bucket] != version:
            print(f"| {bucket} | {deployed[bucket]} -> **{version}** |")
        else:
            print(f"| {bucket} | {version} |")
    return 0


def _bucket_versions(cfg: Config, head: str) -> dict[str, str]:
    from tangier.changemap import buckets, sha_for_bucket

    return {b: sha_for_bucket(cfg, b, head) for b in sorted(buckets(cfg))}


def cmd_deploy(config: Config, args: argparse.Namespace) -> int:
    env = resolve_env(config, args.env)
    runner = _runner(args)
    if runner.which("kubectl") is None:
        raise DeployError("kubectl not found on PATH")

    version_vars = versions(config, args.head)
    ns = env.namespace
    targets = deployment_targets(config)

    print(f"Deploying to {env.name} ({ns})...")
    manifests = _render(runner, config, env, version_vars)

    # Read the prior tag BEFORE applying anything — once pass 1 lands, this
    # reads the new tag. Empty on a first-ever deploy.
    prior = prior_version(runner, env, config)

    job = migration_job_name(env, version_vars)
    if job:
        # Pass 1: the migration Job only.
        result = runner.run(["kubectl", "apply", "-f", "-", "-n", ns, "-l", "migrate-step=pre"], input=manifests)
        if not result.ok:
            # Logged and ignored, exactly as the bash fell through with no set -e.
            print(f"kubectl apply (pass 1) returned {result.returncode}", file=sys.stderr)

        print(f"Waiting for migration job/{job} (timeout {env.migration_timeout}s)...")
        waited = runner.run(
            [
                "kubectl",
                "wait",
                "--for=condition=complete",
                f"--timeout={env.migration_timeout}s",
                f"job/{job}",
                "-n",
                ns,
            ]
        )
        if not waited.ok:
            # No rollback here: nothing has touched the Deployments yet, so the
            # old pods are still serving.
            print("Migration failed; recent logs:")
            _ = runner.run(["kubectl", "logs", "-n", ns, f"job/{job}", "--tail=200"], capture=False)
            return 1

    # Pass 2: everything, from the SAME render — so the Job re-apply is a no-op.
    result = runner.run(["kubectl", "apply", "-f", "-", "-n", ns], input=manifests)
    if not result.ok:
        print(f"kubectl apply (pass 2) returned {result.returncode}", file=sys.stderr)

    if _wait_for_rollout(runner, config, ns, targets):
        print("All deployments rolled out successfully")
        return 0

    print("Rolling back")
    _rollback(runner, config, env, version_vars, prior, targets)
    return 1


def _wait_for_rollout(runner: Runner, cfg: Config, ns: str, targets: list[str]) -> bool:
    """Poll until rolled out, crash-looping, or timed out.

    Elapsed time advances off the value handed to `runner.sleep`, never a real
    clock — so a 600s timeout costs microseconds under a fake runner.
    """
    rollout = cfg.rollout
    elapsed = 0
    while elapsed < rollout.max_wait:
        if runner.run(["kubectl", "rollout", "status", *targets, "-n", ns, "--timeout=1s"]).ok:
            return True

        pods = runner.run(["kubectl", "get", "pods", "-n", ns, "-o", "json"])
        if not pods.ok:
            # Detection is off for this tick; say so rather than silently
            # burning the whole timeout on a rollout that is already crashing.
            print(f"warning: could not list pods (exit {pods.returncode}); crash detection degraded", file=sys.stderr)
        crashing = find_crashing_pods(pods.stdout, rollout.crash_threshold)
        if crashing:
            print("Crash-looping pods detected:")
            for p in crashing:
                print(f"  {p.pod} ({p.container}): {p.reason}, {p.restarts} restarts")
            _ = runner.run(["kubectl", "get", "pods", "-n", ns], capture=False)
            return False

        runner.sleep(rollout.poll_interval)
        elapsed += rollout.poll_interval

    print(f"Deployment timed out after {rollout.max_wait}s")
    return False


def _rollback(
    runner: Runner,
    cfg: Config,
    env,
    version_vars: dict[str, str],
    prior: str,
    targets: list[str],
) -> None:
    """Re-run the prior-tag migration if applicable, then undo each deployment.

    The prior-tag re-migration is a no-op under additive-only schemas, and
    load-bearing for future data migrations tied to a schema version.
    """
    bucket = env.migration_version_bucket
    if bucket and prior and prior != version_vars.get(_var_for(bucket), ""):
        prior_job = migration_job_name(env, {**version_vars, _var_for(bucket): prior})
        if prior_job:
            print(f"Running prior-tag migration {prior_job} before rollout undo")
            _ = runner.run(["kubectl", "delete", "job", prior_job, "-n", env.namespace, "--ignore-not-found"])
            # A *copy* with the migration bucket's var replaced, so the prior
            # version cannot leak into the outer dict — structurally, where the
            # bash used a subshell.
            prior_vars = {**version_vars, _var_for(bucket): prior}
            rendered = _render(runner, cfg, env, prior_vars)
            _ = runner.run(
                ["kubectl", "apply", "-f", "-", "-n", env.namespace, "-l", "migrate-step=pre"], input=rendered
            )
            waited = runner.run(
                [
                    "kubectl",
                    "wait",
                    "--for=condition=complete",
                    f"--timeout={cfg.rollout.rollback_migration_timeout}s",
                    f"job/{prior_job}",
                    "-n",
                    env.namespace,
                ]
            )
            if not waited.ok:
                print("Prior-tag migration failed; proceeding with rollout undo")

    # One at a time, in configured order.
    for target in targets:
        _ = runner.run(["kubectl", "rollout", "undo", target, "-n", env.namespace], capture=False)


def _var_for(bucket: str) -> str:
    from tangier.config import version_var_for

    return version_var_for(bucket)


def add_parsers(sub: argparse._SubParsersAction) -> None:
    dp = sub.add_parser("deploy", help="render and apply k8s manifests for the current image tags")
    _ = dp.add_argument("env", nargs="?", help="environment name from [deploy.<env>]")
    _ = dp.add_argument("--head", default="HEAD")
    _ = dp.add_argument("--render", metavar="ENV", help="print the manifests a deploy would apply, then exit")
    _ = dp.add_argument("--versions", metavar="ENV", help="print the version variables, then exit")
    _ = dp.add_argument(
        "--compare-env",
        metavar="ENV",
        help="markdown build table comparing computed tags against a live environment",
    )
    dp.set_defaults(func=_dispatch)


def _dispatch(config: Config, args: argparse.Namespace) -> int:
    """Route the mutually-exclusive read-only modes before the deploying one."""
    if args.render:
        args.env = args.render
        return cmd_render(config, args)
    if args.versions:
        args.env = args.versions
        return cmd_versions(config, args)
    if args.compare_env:
        return cmd_compare_env(config, args)
    if not args.env:
        print("error: an environment is required (or use --render/--versions/--compare-env)", file=sys.stderr)
        return 2
    return cmd_deploy(config, args)
