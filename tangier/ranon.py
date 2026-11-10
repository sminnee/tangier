"""Where a gate run happened: a dev machine or a CI job.

Each gate pass records this as its `runner`. See `docs/specs/gate.md#store`.
Not `runner`, which is the subprocess seam in `tangier/runner.py`.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping

# Each GitHub Actions field, and the variable it comes from.
GITHUB_FIELDS = {
    "event": "GITHUB_EVENT_NAME",
    "ref": "GITHUB_REF",
    "repository": "GITHUB_REPOSITORY",
    "workflow": "GITHUB_WORKFLOW",
    "job": "GITHUB_JOB",
    "run_id": "GITHUB_RUN_ID",
    "run_attempt": "GITHUB_RUN_ATTEMPT",
    "runner_name": "RUNNER_NAME",
}


# The kinds `TANGIER_RUNNER_KIND` can set. `queue` is a build queue, such as maelstrom's.
KINDS = ("local", "ci", "queue")
KIND_VAR = "TANGIER_RUNNER_KIND"


def in_ci(environ: Mapping[str, str] = os.environ) -> bool:
    """Whether `CI` is `true` or `1`, which GitHub, GitLab, CircleCI and Buildkite all set."""
    return environ.get("CI", "").lower() in ("true", "1")


def kind_override(environ: Mapping[str, str] = os.environ) -> str | None:
    """`TANGIER_RUNNER_KIND`, or None when unset or empty. A value outside `KINDS` raises `ValueError`."""
    value = environ.get(KIND_VAR, "")
    if not value:
        return None
    if value not in KINDS:
        raise ValueError(f"{KIND_VAR}={value} is not a runner kind (use {', '.join(KINDS)})")
    return value


def detect(environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """The runner for a run in this environment.

    `ci` when `in_ci()`, and `local` otherwise. Only GitHub Actions gets more
    than a `provider` of `unknown`. `TANGIER_RUNNER_KIND` then replaces `kind`
    and keeps every other field. A bad value raises `ValueError`.
    """
    runner = _detected(environ)
    override = kind_override(environ)
    if override:
        runner["kind"] = override
    return runner


def _detected(environ: Mapping[str, str]) -> dict[str, str]:
    if not in_ci(environ):
        return {"kind": "local", "host": socket.gethostname()}
    if environ.get("GITHUB_ACTIONS") != "true":
        return {"kind": "ci", "provider": "unknown"}
    runner = {"kind": "ci", "provider": "github-actions"}
    runner.update({field: environ[var] for field, var in GITHUB_FIELDS.items() if environ.get(var)})
    parts = [environ.get(var) for var in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")]
    if all(parts):
        server, repository, run_id = parts
        runner["url"] = f"{server}/{repository}/actions/runs/{run_id}"
    return runner


def load() -> dict[str, float | int]:
    """The 1-minute load average, to 0.1, and the CPU count, or `{}` where the OS gives no load average."""
    try:
        average = os.getloadavg()[0]
    except (OSError, AttributeError):
        return {}
    return {"load": round(average, 1), "cpus": os.cpu_count() or 1}


def overloaded(sample: Mapping[str, float | int] | None) -> bool:
    """Whether a `load()` sample shows more load than CPUs."""
    return bool(sample) and sample["load"] > sample["cpus"]
