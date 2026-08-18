"""`tangier deploy ...` — render, apply in two passes, wait, and roll back on failure.

Exit codes: 0 rolled out; 1 migration or rollout failure (rollback ran);
2 unknown env, bad config, or missing kubectl.
"""

from __future__ import annotations

import argparse
import os
import string
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
from tangier.github import write_summary
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


def compare_table(runner: Runner, config: Config, env, head: str) -> str:
    """The markdown build table, with an `old -> **new**` column for changed buckets.

    Reads the cluster, so it must run BEFORE any apply — pass 1 overwrites
    exactly the tags this compares against.

    Relocated here from changemap: it is the only kubectl dependency, and a
    tag/path tool shelling out to a cluster is the layering violation this
    extraction exists to fix.
    """
    current = {b: v for b, v in _bucket_versions(config, head).items() if not b.endswith("-base")}
    deployed = deployed_versions(runner, config, env.namespace)
    lines = ["## Package builds", "", "| Package | Build ID |", "|-----|-------|"]
    for bucket, version in current.items():
        if bucket in deployed and deployed[bucket] != version:
            lines.append(f"| {bucket} | {deployed[bucket]} -> **{version}** |")
        else:
            lines.append(f"| {bucket} | {version} |")
    return "\n".join(lines) + "\n"


def cmd_compare_env(config: Config, args: argparse.Namespace) -> int:
    """Print the build table for a live environment — a read-only diagnostic."""
    env = resolve_env(config, args.compare_env)
    print(compare_table(_runner(args), config, env, args.head), end="")
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

    # BEFORE any apply: the table reads currently-deployed tags, which pass 1
    # overwrites — and a summary is most wanted when the deploy then fails.
    if getattr(args, "summary", False):
        table = compare_table(runner, config, env, args.head)
        # Falls back to stdout rather than silently doing nothing, so one
        # invocation works both on a runner and on a laptop.
        if not write_summary(table):
            print(table, end="")

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
        # ONLY on success: cutting a release for a version that was just rolled
        # back is worse than not cutting one.
        return _run_after_hook(runner, config, env, version_vars)

    print("Rolling back")
    _rollback(runner, config, env, version_vars, prior, targets)
    return 1


def _run_after_hook(runner: Runner, cfg: Config, env, version_vars: dict[str, str]) -> int:
    """Run `[deploy] after`, if configured. Returns the deploy's exit code.

    The argv was split at parse time; each token is substituted here against
    `ENV` plus the version variables. `safe_substitute` leaves an unknown
    `${...}` alone rather than raising — a hook is a side errand, and a typo in
    one should not fail a deploy that has already rolled out.
    """
    hook = cfg.after
    if hook is None:
        return 0
    mapping = {"ENV": env.name, **version_vars}
    argv = [string.Template(token).safe_substitute(mapping) for token in hook.argv]
    print(f"Running post-deploy hook: {' '.join(argv)}")
    result = runner.run(argv, capture=False, env={**os.environ, **version_vars, "ENV": env.name})
    if result.ok:
        return 0
    if hook.fatal:
        print(f"post-deploy hook failed (exit {result.returncode})", file=sys.stderr)
        return result.returncode
    print(f"post-deploy hook failed (exit {result.returncode}); non-fatal", file=sys.stderr)
    return 0


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
    _ = dp.add_argument(
        "--summary",
        action="store_true",
        help="write the build table to $GITHUB_STEP_SUMMARY (or stdout) before deploying",
    )
    dp.set_defaults(func=_dispatch)


def _dispatch(config: Config, args: argparse.Namespace) -> int:
    """Route the mutually-exclusive read-only modes before the deploying one."""
    # Rejected rather than ignored: `--summary` decorates a deploy, and silently
    # dropping it from a read-only mode would look like it had run.
    if args.summary and (args.render or args.versions or args.compare_env):
        print("error: --summary cannot be combined with --render/--versions/--compare-env", file=sys.stderr)
        return 2
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
