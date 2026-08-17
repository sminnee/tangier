"""Two-pass kustomize deploy with migration gating, crash detection and rollback.

Ported from a bash script that deliberately had no `set -e`. That is preserved:
`kubectl apply` returncodes are logged and ignored exactly as the bash fell
through, so a partial apply proceeds and is caught at rollout-status. Changing
that is a behaviour change on the riskiest path and deserves its own task.

No module here imports `time` or `subprocess` — everything external goes
through a Runner, and the poll loop's clock advances off the value passed to
`runner.sleep(...)`.
"""

from __future__ import annotations

import json
import string
from dataclasses import dataclass

from tangier.changemap import buckets, sha_for_bucket
from tangier.config import Config, DeployEnv
from tangier.image import parse_image_tag
from tangier.runner import Runner


class DeployError(RuntimeError):
    """A problem that should exit non-zero before touching the cluster."""


def resolve_env(cfg: Config, name: str) -> DeployEnv:
    """Look up an environment by name.

    An unknown name is an error listing the known ones. The bash treated any
    argument but `prod` as uat, so a one-character workflow typo silently
    retargeted an environment *and succeeded*.
    """
    env = cfg.deploy_envs.get(name)
    if env is None:
        known = ", ".join(sorted(cfg.deploy_envs)) or "(none configured)"
        raise DeployError(f"unknown environment: {name} (known: {known})")
    return env


def versions(cfg: Config, head: str = "HEAD") -> dict[str, str]:
    """Every bucket's version variable -> content hash."""
    from tangier.config import version_var_for

    return {version_var_for(b): sha_for_bucket(cfg, b, head) for b in sorted(buckets(cfg))}


def envsubst_allowlist(cfg: Config) -> str:
    """The `envsubst` variable allowlist, derived from config.

    Every bucket's version variable, sorted. envsubst treats it as a set, so the
    order is cosmetic — sorted just makes it diffable. Deriving it removes the
    hand-maintained duplicate that had to be edited in two places whenever a
    service was added.
    """
    from tangier.config import version_var_for

    return " ".join(f"${version_var_for(b)}" for b in sorted(buckets(cfg)))


def render_argv(env: DeployEnv, allowlist: str) -> list[list[str]]:
    """The `kubectl kustomize | envsubst` pipeline stages."""
    return [["kubectl", "kustomize", env.overlay], ["envsubst", allowlist]]


def migration_job_name(env: DeployEnv, version_vars: dict[str, str]) -> str | None:
    """The version-embedded migration Job name, or None if this env has no migration.

    `string.Template` rather than f-string interpolation so the config carries
    `${SMARTYPANTS_VERSION}` literally — which is what keeps the pass-2 re-apply
    a genuine no-op.

    An unresolved placeholder is an error. Left alone it produces a job name no
    cluster can have, and the deploy then waits out the whole migration timeout
    before failing as if the migration itself had broken.
    """
    if not env.migration_job:
        return None
    template = string.Template(env.migration_job)
    unresolved = [m.group("named") or m.group("braced") for m in template.pattern.finditer(env.migration_job)]
    missing = sorted({v for v in unresolved if v and v not in version_vars})
    if missing:
        known = ", ".join(sorted(version_vars)) or "(none)"
        raise DeployError(
            f"`[deploy.{env.name}].migration_job` references unknown "
            f"variable{'s' if len(missing) > 1 else ''} {', '.join(missing)} (known: {known})"
        )
    return template.safe_substitute(version_vars)


@dataclass
class PodProblem:
    pod: str
    container: str
    reason: str
    restarts: int


def find_crashing_pods(pods_json: str, threshold: int) -> list[PodProblem]:
    """Flag pods whose containers are crash-looping past `threshold` restarts.

    Parsed in Python rather than shelled out because the original
    `jsonpath | grep | awk` pipeline is position-dependent: an empty
    `state.waiting.reason` shifts every subsequent field, so it silently
    mis-parses. The reason and the restart count must belong to the *same*
    container, which a field-offset scan cannot guarantee.

    This is the one place shell is replaced rather than wrapped, because the
    shell version is subtly wrong rather than merely ugly.
    """
    try:
        data = json.loads(pods_json or "{}")
    except json.JSONDecodeError:
        return []
    problems: list[PodProblem] = []
    for item in data.get("items", []):
        name = item.get("metadata", {}).get("name", "")
        status = item.get("status", {})
        for key in ("containerStatuses", "initContainerStatuses"):
            for cs in status.get(key) or []:
                reason = (cs.get("state", {}).get("waiting", {}) or {}).get("reason", "")
                restarts = cs.get("restartCount", 0) or 0
                if reason in ("CrashLoopBackOff", "OOMKilled") and restarts >= threshold:
                    problems.append(
                        PodProblem(pod=name, container=cs.get("name", ""), reason=reason, restarts=restarts)
                    )
    return problems


def deployment_targets(cfg: Config) -> list[str]:
    """`deployment/<name>` for every configured k8s workload, in config order."""
    out: list[str] = []
    for bucket in sorted(cfg.k8s):
        for dep in cfg.k8s[bucket].deployments:
            out.append(f"deployment/{dep}")
    return out


def prior_version(runner: Runner, env: DeployEnv, cfg: Config) -> str:
    """The currently-deployed tag of the migration bucket, read BEFORE any apply.

    Ordering is load-bearing — once pass 1 applies, this reads the new tag.
    Empty on a first-ever deploy.
    """
    bucket = env.migration_version_bucket
    if not bucket:
        return ""
    spec = cfg.k8s.get(bucket)
    if spec is None or not spec.deployments:
        return ""
    deployment = spec.deployments[0]
    jsonpath = f'{{.spec.template.spec.containers[?(@.name=="{spec.container}")].image}}'
    result = runner.run(["kubectl", "get", "deployment", deployment, "-n", env.namespace, "-o", f"jsonpath={jsonpath}"])
    if not result.ok:
        return ""
    return parse_image_tag(result.stdout.strip())


def deployed_versions(runner: Runner, cfg: Config, namespace: str) -> dict[str, str]:
    """bucket -> currently-deployed tag, for every configured bucket.

    Driven by `[k8s.*]` rather than a hand-maintained dict, which is what fixes
    the gap where a service present in the config was silently absent from the
    comparison.
    """
    names = [spec.deployments[0] for spec in cfg.k8s.values() if spec.deployments]
    if not names:
        return {}
    result = runner.run(["kubectl", "get", "deployment", "-n", namespace, *names, "-o", "json"])
    if not result.ok:
        return {}
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    # The image-carrying container is not always first — that is what
    # `[k8s.*] container` exists to say — so index the raw containers and let
    # each bucket pick its own by name.
    containers_by_deployment: dict[str, list[dict]] = {}
    for item in data.get("items", []):
        name = item.get("metadata", {}).get("name", "")
        containers_by_deployment[name] = item.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    out: dict[str, str] = {}
    for bucket, spec in cfg.k8s.items():
        if not spec.deployments:
            continue
        containers = containers_by_deployment.get(spec.deployments[0])
        if not containers:
            continue
        chosen = next((c for c in containers if c.get("name") == spec.container), containers[0])
        out[bucket] = parse_image_tag(chosen.get("image", ""))
    return out
