"""`pipeline.toml` -> Config.

The file describes a view of the repo as tags over globs, plus reserved
sections configuring the image and deploy phases. Resolution is
ignore-by-default: the TOML is an opt-in list of paths that matter, not an
exhaustive map of the repo.

Tag tables carry:
  paths         (required-ish, str | list of str) — globs of files belonging to
                this tag. Optional: a tag with only `depends` is a valid aggregator.
  exclude       (optional, str | list of str) — globs subtracted from `paths`.
                A file belongs to the tag iff it matches `paths` and NOT
                `exclude`. Per-tag: an excluded path still matches other tags.
                Also filters the tag's SHA bucket, so match set and hash agree.
  depends       (optional, str | list of str) — tag names; reverse-transitive
                expansion: when X changes, also run every tag whose dependency
                closure transitively contains X.
  <name>_items  (optional, str | list of str) — file-list selector. When this
                tag is in the expanded set, contribute these paths to the named
                list. `_items` is the marker suffix.
  sha           (optional, true | "bucket-name") — `true` means this tag IS a
                SHA bucket of the same name; a string names the bucket it
                contributes to.
  touched       (optional, bool) — emit `<tag>-touched=true|false`.
  files         (optional, true) — marks a projection-only file-set table.

Tags may be written bare (`[mytag]`) or nested (`[tags.mytag]`). Nesting is how
you name a tag that collides with a reserved section.
"""

from __future__ import annotations

import re
import shlex
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# Top-level names that configure tangier rather than declaring a tag. Reserved
# now even where the behaviour lands later: reserving a name is cheap,
# un-reserving one after a config author used it as a tag is a breaking change.
RESERVED_SECTIONS = frozenset({"tags", "sha", "registry", "deploy", "image", "k8s", "runners", "tailnet", "gate"})

# Recognised tag-table fields. `_items` is matched by suffix.
_KNOWN_FIELDS = {"paths", "exclude", "depends", "sha", "touched", "files"}

# `{<name>-items}` in a gate command is the items list `<name>`, the same name
# `changemap github-outputs` emits. Any other `{<group>}` is a file-set group.
ITEMS_PLACEHOLDER_SUFFIX = "-items"

# Excluded from every bucket SHA unless `[sha] exclude` overrides it. Documentation
# changes must not rebuild images.
DEFAULT_SHA_EXCLUDE = ["**/README.md"]


class ConfigError(ValueError):
    """A malformed config. Carries a message naming the file and the offending key."""


@dataclass
class ShaSettings:
    """`[sha]` — how bucket content hashes are computed."""

    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_SHA_EXCLUDE))


@dataclass
class RunnerSpec:
    """`[runners.<items-name>]` — how `explain` renders an items name's invocation.

    `files` names the file-set group feeding this runner's `--files` flag. It is
    explicit because the link is not derivable: the items name is `unittest`
    while the file-set table is `unittest-files`. Omitting `files` means the
    runner never receives a `--files` suffix.
    """

    cmd: str
    files: str | None = None


@dataclass
class ImageSpec:
    """`[image.<bucket>]` — how to build a bucket's image."""

    dockerfile: str
    context: str = "."
    platform: str | None = None
    cache: bool = True
    args: dict[str, str] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)


@dataclass
class K8sSpec:
    """`[k8s.<bucket>]` — the cluster objects a bucket's image backs.

    Top-level and keyed by bucket rather than nested per environment: every
    environment renders from the same base kustomization, so per-env placement
    would only invite drift. It also expresses the one-to-many relation (one
    bucket driving several deployments) that a flat per-env list cannot.
    """

    deployments: list[str]
    container: str = "server"
    version_var: str = ""


@dataclass
class DeployEnv:
    """`[deploy.<env>]` — one deployable environment."""

    name: str
    namespace: str
    overlay: str
    migration_timeout: int = 600
    migration_job: str | None = None
    migration_version_bucket: str | None = None


@dataclass
class RolloutSettings:
    """`[deploy.rollout]` — shared rollout/rollback tuning."""

    max_wait: int = 600
    poll_interval: int = 10
    crash_threshold: int = 3
    rollback_migration_timeout: int = 600


@dataclass
class AfterHook:
    """`[deploy] after` — a command to run once a deploy has fully rolled out.

    Split into argv at PARSE time with `shlex`, before any substitution, so an
    environment name containing a space cannot inject an argument and
    `runner.run` never needs a shell. Each token is then substituted
    individually against the version variables.

    `fatal` defaults to False: the hook is a notification (cut a release, ping a
    channel), and a successful deploy should not be reported as failed because
    a side errand did not land.
    """

    argv: list[str]
    fatal: bool = False


@dataclass
class TailnetEnv:
    """`[tailnet.<env>]` — the tailnet ACL tag a deploy to this env authenticates as."""

    name: str
    tag: str


@dataclass
class TailnetSettings:
    """`[tailnet]` — how CI reaches the cluster over the tailnet.

    `operator` is the Tailscale Kubernetes operator's hostname, which appears in
    the kubeconfig context name. All four consuming repos use the default.
    """

    operator: str = "tailscale-operator"
    envs: dict[str, TailnetEnv] = field(default_factory=dict)


@dataclass
class GateSpec:
    """`[gate.<name>]` — a set of commands whose pass is recorded under a key of the content it tested.

    `commands` are split into argv at PARSE time, as `AfterHook` is, so
    `runner.run` never needs a shell. A `{...}` placeholder is a whole token and
    is replaced at run time by a comma-joined list from the answer set.

    `scope` names packages: a SHA bucket, or a tag that lists `paths`. The gate
    has no path list of its own.
    """

    commands: list[list[str]]
    env: dict[str, str]
    scope: list[str]
    # The group's name, for a member of `[gate.<group>.<name>]`. Its full name is `<group>.<name>`.
    group: str | None = None


@dataclass
class Config:
    paths: dict[str, list[str]] = field(default_factory=dict)
    # tag -> globs subtracted from that tag's `paths` (absent == no exclusions).
    exclude: dict[str, list[str]] = field(default_factory=dict)
    depends: dict[str, list[str]] = field(default_factory=dict)
    # items name -> tag -> paths.
    items: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # tag -> bucket name (== tag name when `sha = true`).
    sha_bucket: dict[str, str] = field(default_factory=dict)
    # Tags that opt into a `-touched` flag in github-outputs.
    touched: set[str] = field(default_factory=set)
    # Projection-only file-set tables: group name -> globs over the raw diff.
    file_sets: dict[str, list[str]] = field(default_factory=dict)

    sha: ShaSettings = field(default_factory=ShaSettings)
    registry: str = ""
    runners: dict[str, RunnerSpec] = field(default_factory=dict)
    images: dict[str, ImageSpec] = field(default_factory=dict)
    k8s: dict[str, K8sSpec] = field(default_factory=dict)
    deploy_envs: dict[str, DeployEnv] = field(default_factory=dict)
    rollout: RolloutSettings = field(default_factory=RolloutSettings)
    after: AfterHook | None = None
    tailnet: TailnetSettings = field(default_factory=TailnetSettings)
    gates: dict[str, GateSpec] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _coerce_str_or_list(path: str, tag: str, key: str, val: object) -> list[str]:
    """Normalise a `str | list[str]` field to a list of strings."""
    if isinstance(val, str):
        return [val]
    if isinstance(val, list) and all(isinstance(v, str) for v in val):
        return list(val)
    raise ConfigError(f"{path}: `[{tag}].{key}` must be a string or list of strings")


def _require_table(path: str, name: str, val: object) -> dict[str, Any]:
    """Require a reserved section to be a table.

    The `[tags.<name>]` escape hatch is only mentioned for a TOP-LEVEL reserved
    name, since that is the only place it applies — suggesting `[tags.deploy.uat]`
    for a malformed deploy environment would send the author somewhere worse.
    """
    if isinstance(val, dict):
        return val
    message = f"{path}: `[{name}]` is a reserved section and must be a table, got {type(val).__name__}."
    if "." not in name:
        message += f" To declare a tag with this name, write `[tags.{name}]`."
    raise ConfigError(message)


def _check_keys(path: str, section: str, body: dict[str, Any], allowed: set[str]) -> None:
    for k in body:
        if k not in allowed:
            raise ConfigError(f"{path}: unknown field `{k}` on `[{section}]` (allowed: {', '.join(sorted(allowed))})")


def _as_int(path: str, section: str, key: str, val: object) -> int:
    if isinstance(val, bool) or not isinstance(val, int):
        raise ConfigError(f"{path}: `[{section}].{key}` must be an integer")
    return val


def _as_str(path: str, section: str, key: str, val: object) -> str:
    if not isinstance(val, str):
        raise ConfigError(f"{path}: `[{section}].{key}` must be a string")
    return val


# ---------------------------------------------------------------------------
# Reserved-section handlers
# ---------------------------------------------------------------------------


def _parse_sha(path: str, body: dict[str, Any]) -> ShaSettings:
    _check_keys(path, "sha", body, {"exclude"})
    if "exclude" not in body:
        return ShaSettings()
    # An explicit `exclude = []` disables filtering entirely, and must stay
    # distinguishable from an absent section (which gets the default).
    return ShaSettings(exclude=_coerce_str_or_list(path, "sha", "exclude", body["exclude"]))


def _parse_registry(path: str, body: dict[str, Any]) -> str:
    _check_keys(path, "registry", body, {"url"})
    if "url" not in body:
        raise ConfigError(f"{path}: `[registry]` requires `url`")
    return _as_str(path, "registry", "url", body["url"]).rstrip("/")


def _parse_runners(path: str, body: dict[str, Any]) -> dict[str, RunnerSpec]:
    out: dict[str, RunnerSpec] = {}
    for name, spec in body.items():
        spec = _require_table(path, f"runners.{name}", spec)
        _check_keys(path, f"runners.{name}", spec, {"cmd", "files"})
        if "cmd" not in spec:
            raise ConfigError(f"{path}: `[runners.{name}]` requires `cmd`")
        files = spec.get("files")
        if files is not None and not isinstance(files, str):
            raise ConfigError(f"{path}: `[runners.{name}].files` must be a string")
        out[name] = RunnerSpec(cmd=_as_str(path, f"runners.{name}", "cmd", spec["cmd"]), files=files)
    return out


def _parse_images(path: str, body: dict[str, Any]) -> dict[str, ImageSpec]:
    out: dict[str, ImageSpec] = {}
    for bucket, spec in body.items():
        spec = _require_table(path, f"image.{bucket}", spec)
        _check_keys(path, f"image.{bucket}", spec, {"dockerfile", "context", "platform", "cache", "args", "secrets"})
        if "dockerfile" not in spec:
            raise ConfigError(f"{path}: `[image.{bucket}]` requires `dockerfile`")
        cache = spec.get("cache", True)
        if not isinstance(cache, bool):
            raise ConfigError(f"{path}: `[image.{bucket}].cache` must be a boolean")
        args = spec.get("args", {})
        if not isinstance(args, dict) or not all(isinstance(v, str) for v in args.values()):
            raise ConfigError(f"{path}: `[image.{bucket}].args` must be a table of strings")
        platform = spec.get("platform")
        if platform is not None and not isinstance(platform, str):
            raise ConfigError(f"{path}: `[image.{bucket}].platform` must be a string")
        out[bucket] = ImageSpec(
            dockerfile=_as_str(path, f"image.{bucket}", "dockerfile", spec["dockerfile"]),
            context=_as_str(path, f"image.{bucket}", "context", spec.get("context", ".")),
            platform=platform,
            cache=cache,
            args=dict(args),
            secrets=_coerce_str_or_list(path, f"image.{bucket}", "secrets", spec["secrets"])
            if "secrets" in spec
            else [],
        )
    return out


def _parse_k8s(path: str, body: dict[str, Any]) -> dict[str, K8sSpec]:
    out: dict[str, K8sSpec] = {}
    for bucket, spec in body.items():
        spec = _require_table(path, f"k8s.{bucket}", spec)
        _check_keys(path, f"k8s.{bucket}", spec, {"deployments", "container", "version_var"})
        deployments = (
            _coerce_str_or_list(path, f"k8s.{bucket}", "deployments", spec["deployments"])
            if "deployments" in spec
            else [bucket]
        )
        out[bucket] = K8sSpec(
            deployments=deployments,
            container=_as_str(path, f"k8s.{bucket}", "container", spec.get("container", "server")),
            version_var=_as_str(path, f"k8s.{bucket}", "version_var", spec.get("version_var", "")),
        )
    return out


# Shell operators. The hook runs WITHOUT a shell, so an author who writes one
# means something the hook cannot do — it would reach the command as a literal
# argument and fail somewhere strange, mid-deploy. Rejecting at parse time means
# they learn about it before a deploy rather than during one.
#
# Matched against whole TOKENS, after splitting: `>` is an operator on its own
# and harmless inside `--title=a>b`, which never reaches a shell to be
# redirected. `${...}` is the hook's own substitution syntax and is not an
# operator; `$(` and a backtick are, since neither can ever be substituted.
_SHELL_OPERATOR_TOKENS = frozenset({"&&", "||", "|", ";", ">", ">>", "<", "&"})
_SHELL_SUBSTITUTIONS = ("$(", "`")


def _split_command(path: str, label: str, cmd: str) -> list[str]:
    """Split a shell-free command line into argv, or raise naming `label`."""
    for marker in _SHELL_SUBSTITUTIONS:
        if marker in cmd:
            raise ConfigError(
                f"{path}: {label} contains `{marker}`, but the command runs without a shell "
                f"— put the logic in a script and call that instead"
            )
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        raise ConfigError(f"{path}: {label} is not a valid command line: {e}") from e
    if not argv:
        raise ConfigError(f"{path}: {label} is empty")
    for token in argv:
        if token in _SHELL_OPERATOR_TOKENS:
            raise ConfigError(
                f"{path}: {label} contains the shell operator `{token}`, but the command "
                f"runs without a shell — put the logic in a script and call that instead"
            )
    return argv


def _parse_after(path: str, val: object) -> AfterHook:
    """Parse `[deploy] after` — the string shorthand or the explicit table.

    `after = "bin/x ${ENV}"` is sugar for a table with `fatal = false`, which is
    the shape every current consumer wants.
    """
    if isinstance(val, str):
        cmd, fatal = val, False
    elif isinstance(val, dict):
        _check_keys(path, "deploy.after", val, {"cmd", "fatal"})
        if "cmd" not in val:
            raise ConfigError(f"{path}: `[deploy.after]` requires `cmd`")
        cmd = _as_str(path, "deploy.after", "cmd", val["cmd"])
        fatal = val.get("fatal", False)
        if not isinstance(fatal, bool):
            raise ConfigError(f"{path}: `[deploy.after].fatal` must be a boolean")
    else:
        raise ConfigError(f"{path}: `[deploy] after` must be a string or a table, got {type(val).__name__}")

    return AfterHook(argv=_split_command(path, "`[deploy] after`", cmd), fatal=fatal)


def _parse_deploy(path: str, body: dict[str, Any]) -> tuple[dict[str, DeployEnv], RolloutSettings, AfterHook | None]:
    envs: dict[str, DeployEnv] = {}
    rollout = RolloutSettings()
    after: AfterHook | None = None
    for name, spec in body.items():
        # `after` first, BEFORE `_require_table`: its string shorthand is the
        # documented form, and `_require_table` would reject it with a message
        # that is both wrong and unactionable.
        if name == "after":
            after = _parse_after(path, spec)
            continue
        spec = _require_table(path, f"deploy.{name}", spec)
        if name == "rollout":
            _check_keys(
                path,
                "deploy.rollout",
                spec,
                {"max_wait", "poll_interval", "crash_threshold", "rollback_migration_timeout"},
            )
            rollout = RolloutSettings(
                max_wait=_as_int(path, "deploy.rollout", "max_wait", spec.get("max_wait", 600)),
                poll_interval=_as_int(path, "deploy.rollout", "poll_interval", spec.get("poll_interval", 10)),
                crash_threshold=_as_int(path, "deploy.rollout", "crash_threshold", spec.get("crash_threshold", 3)),
                rollback_migration_timeout=_as_int(
                    path, "deploy.rollout", "rollback_migration_timeout", spec.get("rollback_migration_timeout", 600)
                ),
            )
            continue
        _check_keys(
            path,
            f"deploy.{name}",
            spec,
            {"namespace", "overlay", "migration_timeout", "migration_job", "migration_version_bucket"},
        )
        for required in ("namespace", "overlay"):
            if required not in spec:
                raise ConfigError(f"{path}: `[deploy.{name}]` requires `{required}`")
        migration_job = spec.get("migration_job")
        if migration_job is not None and not isinstance(migration_job, str):
            raise ConfigError(f"{path}: `[deploy.{name}].migration_job` must be a string")
        bucket = spec.get("migration_version_bucket")
        if bucket is not None and not isinstance(bucket, str):
            raise ConfigError(f"{path}: `[deploy.{name}].migration_version_bucket` must be a string")
        envs[name] = DeployEnv(
            name=name,
            namespace=_as_str(path, f"deploy.{name}", "namespace", spec["namespace"]),
            overlay=_as_str(path, f"deploy.{name}", "overlay", spec["overlay"]),
            migration_timeout=_as_int(path, f"deploy.{name}", "migration_timeout", spec.get("migration_timeout", 600)),
            migration_job=migration_job,
            migration_version_bucket=bucket,
        )
    return envs, rollout, after


# Each part of a gate name becomes part of a ref component
# (`refs/tangier/gates/<group>.<name>/<key>`) and of an output name
# (`<group>-<name>-verified`), so it must be valid as both.
_GATE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")
_GATE_KEYS = {"cmd", "env", "scope"}


def gate_output_name(name: str) -> str:
    """The GitHub output prefix for a gate or group: `frontend.lint` gives `frontend-lint`."""
    return name.replace(".", "-")


def gate_groups(cfg: Config) -> dict[str, list[str]]:
    """Each group name, mapped to its members' full names, in config order."""
    groups: dict[str, list[str]] = {}
    for name, spec in cfg.gates.items():
        if spec.group:
            groups.setdefault(spec.group, []).append(name)
    return groups


def _parse_gates(path: str, body: dict[str, Any]) -> dict[str, GateSpec]:
    """Parse `[gate]`: a table with `cmd` is a gate, and a table without one is a group of gates.

    A group's `env` and `scope` are merged into each member here, so every
    later reader sees one resolved `GateSpec`.
    """
    out: dict[str, GateSpec] = {}
    for name, spec in body.items():
        section = f"gate.{name}"
        _check_gate_name(path, section, name)
        spec = _require_table(path, section, spec)
        if "cmd" in spec:
            out[name] = _parse_gate(path, section, spec, None, {}, [])
            continue
        members = {k: v for k, v in spec.items() if k not in ("env", "scope")}
        if not members:
            raise ConfigError(f"{path}: `[{section}]` requires `cmd`, or member gates to make it a group")
        for member, value in members.items():
            if not isinstance(value, dict):
                raise ConfigError(
                    f"{path}: `[{section}]` has no `cmd`, so it is a group, which takes `env`, `scope` "
                    f"and member tables only, not `{member}`"
                )
        group_env = _gate_env(path, section, spec)
        group_scope = _coerce_str_or_list(path, section, "scope", spec.get("scope", []))
        for member, value in members.items():
            member_section = f"{section}.{member}"
            _check_gate_name(path, member_section, member)
            if member == "lock":
                # Git refuses a ref component that ends in `.lock`, so the record could never be written.
                raise ConfigError(f"{path}: `[{member_section}]`: a member cannot be named `lock`")
            if "cmd" not in value:
                nested = any(isinstance(v, dict) for v in value.values())
                raise ConfigError(
                    f"{path}: `[{member_section}]` requires `cmd`" + (": groups nest one level only" if nested else "")
                )
            out[f"{name}.{member}"] = _parse_gate(path, member_section, value, name, group_env, group_scope)
    _check_gate_output_names(path, out)
    return out


def _check_gate_name(path: str, section: str, name: str) -> None:
    if not _GATE_NAME.fullmatch(name):
        raise ConfigError(
            f"{path}: `[{section}]` is not a valid gate name (letters, digits, `-` and `_`, not starting with `-`)"
        )


def _gate_env(path: str, section: str, spec: dict[str, Any]) -> dict[str, str]:
    env = spec.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        raise ConfigError(f"{path}: `[{section}].env` must be a table of strings")
    return dict(env)


def _parse_gate(
    path: str,
    section: str,
    spec: dict[str, Any],
    group: str | None,
    group_env: dict[str, str],
    group_scope: list[str],
) -> GateSpec:
    """One gate. A member takes its group's `env` beneath its own, and its group's `scope` ahead of its own."""
    _check_keys(path, section, spec, _GATE_KEYS)
    if not group_scope and "scope" not in spec:
        raise ConfigError(f"{path}: `[{section}]` requires `scope`")
    cmds = _coerce_str_or_list(path, section, "cmd", spec["cmd"])
    own_scope = _coerce_str_or_list(path, section, "scope", spec.get("scope", []))
    scope = list(dict.fromkeys([*group_scope, *own_scope]))
    for key, val in (("cmd", cmds), ("scope", scope)):
        if not val:
            raise ConfigError(f"{path}: `[{section}]` `{key}` is empty")
    return GateSpec(
        commands=[_split_command(path, f"`[{section}] cmd`", cmd) for cmd in cmds],
        env={**group_env, **_gate_env(path, section, spec)},
        scope=scope,
        group=group,
    )


def _check_gate_output_names(path: str, gates: dict[str, GateSpec]) -> None:
    """Reject two gates, or a gate and a group, whose GitHub outputs would share a name."""
    seen: dict[str, str] = {}
    names = [*gates, *dict.fromkeys(spec.group for spec in gates.values() if spec.group)]
    for name in names:
        output = gate_output_name(name)
        if output in seen:
            raise ConfigError(
                f"{path}: `[gate.{seen[output]}]` and `[gate.{name}]` share the output name `{output}-run`; rename one"
            )
        seen[output] = name


def _parse_tailnet(path: str, body: dict[str, Any]) -> TailnetSettings:
    """Parse `[tailnet]` — a scalar `operator` plus one table per environment.

    Split by TYPE, not by name: a scalar is a setting on `[tailnet]` itself, a
    table is an environment. `[deploy.rollout]` special-cases a reserved *name*,
    which does not apply here — an environment may legitimately be called
    anything, and the two kinds are never ambiguous.
    """
    settings = TailnetSettings()
    for key, value in body.items():
        if isinstance(value, dict):
            _check_keys(path, f"tailnet.{key}", value, {"tag"})
            if "tag" not in value:
                # A `[tailnet.<env>]` with nothing in it is a typo, not a way to
                # say "this env needs no tag" — there is no such thing.
                raise ConfigError(f"{path}: `[tailnet.{key}]` requires `tag`")
            settings.envs[key] = TailnetEnv(name=key, tag=_as_str(path, f"tailnet.{key}", "tag", value["tag"]))
        elif key == "operator":
            settings.operator = _as_str(path, "tailnet", "operator", value)
        else:
            raise ConfigError(
                f"{path}: unknown field `{key}` on `[tailnet]` (allowed: operator, or a `[tailnet.<env>]` table)"
            )
    return settings


# ---------------------------------------------------------------------------
# Tag parsing
# ---------------------------------------------------------------------------


def _parse_tag(cfg: Config, path: str, tag: str, body: dict[str, Any], raw_depends: dict[str, list[str]]) -> None:
    """Register one tag table. Unchanged from the pre-extraction parser."""
    for k in body:
        if k.endswith("_items"):
            continue
        if k not in _KNOWN_FIELDS:
            raise ConfigError(f"{path}: unknown field `{k}` on `[{tag}]`")
    # `files = true` marks a projection-only file-set table: record its globs
    # under file_sets and skip all tag-match registration, so it never enters
    # paths/matched/expanded, SHA, touched, or ignored.
    if "files" in body:
        if body["files"] is not True:
            raise ConfigError(f"{path}: `[{tag}].files` must be `true`")
        # A file-set table's globs are already an explicit allowlist over the raw
        # diff, so there is nothing for `exclude` to subtract from.
        if "exclude" in body:
            raise ConfigError(f"{path}: `exclude` is not supported on the `files = true` table `[{tag}]`")
        # A file-set is never selected, so a tag field here would be silently ignored.
        for k in body:
            if k not in ("files", "paths"):
                raise ConfigError(f"{path}: `{k}` is a tag field, not supported on the `files = true` table `[{tag}]`")
        cfg.file_sets[tag] = _coerce_str_or_list(path, tag, "paths", body["paths"]) if "paths" in body else []
        return
    # paths is optional; tags with only `depends` are valid aggregators.
    cfg.paths[tag] = _coerce_str_or_list(path, tag, "paths", body["paths"]) if "paths" in body else []
    if "exclude" in body:
        cfg.exclude[tag] = _coerce_str_or_list(path, tag, "exclude", body["exclude"])
    for k, v in body.items():
        if k.endswith("_items"):
            name = k[: -len("_items")]
            if not name:
                raise ConfigError(f"{path}: empty items name on `[{tag}].{k}`")
            cfg.items.setdefault(name, {})[tag] = _coerce_str_or_list(path, tag, k, v)
    if "sha" in body:
        sha_val = body["sha"]
        if sha_val is True:
            cfg.sha_bucket[tag] = tag
        elif isinstance(sha_val, str):
            cfg.sha_bucket[tag] = sha_val
        else:
            raise ConfigError(f"{path}: `[{tag}].sha` must be `true` or a string bucket name")
    if "touched" in body:
        touched_val = body["touched"]
        if not isinstance(touched_val, bool):
            raise ConfigError(f"{path}: `[{tag}].touched` must be a boolean")
        if touched_val:
            cfg.touched.add(tag)
    if "depends" in body:
        raw_depends[tag] = _coerce_str_or_list(path, tag, "depends", body["depends"])


def _check_no_cycles(depends: dict[str, list[str]], config_path: str) -> None:
    """DFS each node, raising if a back-edge into the current stack is found."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = dict.fromkeys(depends, WHITE)

    def visit(tag: str, stack: list[str]) -> None:
        colour[tag] = GREY
        stack.append(tag)
        for dep in depends.get(tag, []):
            if colour.get(dep) == GREY:
                cycle = stack[stack.index(dep) :] + [dep]
                raise ConfigError(f"{config_path}: dependency cycle: {' -> '.join(cycle)}")
            if colour.get(dep) == WHITE:
                visit(dep, stack)
        _ = stack.pop()
        colour[tag] = BLACK

    for tag in list(depends.keys()):
        if colour[tag] == WHITE:
            visit(tag, [])


def _warn_tags_without_input(cfg: Config, path: str) -> None:
    """Tags with no `paths` and no `depends` can never enter the matched set."""
    for tag in cfg.paths:
        if not cfg.paths[tag] and not cfg.depends.get(tag):
            print(
                f"{path}: warning: `[{tag}]` has neither `paths` nor `depends` — it will never trigger",
                file=sys.stderr,
            )


def _warn_tags_without_output(cfg: Config, path: str) -> None:
    """Tags with no sha bucket, no touched flag, and no _items projection produce no observable output."""
    items_tags = {tag for tag_map in cfg.items.values() for tag in tag_map}
    # Tags something else depends ON count as having output: their presence in
    # the graph propagates expansion to dependents that DO have output.
    depended_upon = {dep for deps in cfg.depends.values() for dep in deps}
    gated = {tag for gate in cfg.gates.values() for tag in scope_tags(cfg, gate)}
    for tag in cfg.paths:
        has_output = (
            tag in cfg.sha_bucket or tag in cfg.touched or tag in items_tags or tag in depended_upon or tag in gated
        )
        if not has_output:
            print(
                f"{path}: warning: `[{tag}]` has no sha, touched, *_items, gate scope, "
                "and nothing depends on it — no CI signal",
                file=sys.stderr,
            )


def _warn_tailnet_envs_without_deploy(cfg: Config, path: str) -> None:
    """Warn when a `[tailnet.<env>]` names no `[deploy.<env>]`.

    A warning, not an error, and only when there is something to compare
    against: k8s-cluster declares zero deploy environments and still wants the
    tailnet action, so an empty `[deploy]` means "not a deploying repo" rather
    than "every tailnet env is a typo".
    """
    if not cfg.deploy_envs:
        return
    for name in sorted(cfg.tailnet.envs):
        if name not in cfg.deploy_envs:
            known = ", ".join(sorted(cfg.deploy_envs))
            print(
                f"{path}: warning: `[tailnet.{name}]` names no deploy environment (known: {known})",
                file=sys.stderr,
            )


def _derive(cfg: Config, path: str) -> None:
    """Fill in what config need not state, and reject what it cannot mean.

    `[k8s.*]` keys must name real SHA buckets — a typo there is otherwise a
    silent deploy failure, since the version variable it derives would never be
    substituted into any manifest.
    """
    known_buckets = set(cfg.sha_bucket.values())
    for bucket, spec in cfg.k8s.items():
        if bucket not in known_buckets:
            known = ", ".join(sorted(known_buckets)) or "(none)"
            raise ConfigError(f"{path}: `[k8s.{bucket}]` does not name a SHA bucket (known buckets: {known})")
        if not spec.version_var:
            spec.version_var = version_var_for(bucket)
    for bucket in cfg.images:
        if bucket not in known_buckets:
            known = ", ".join(sorted(known_buckets)) or "(none)"
            raise ConfigError(f"{path}: `[image.{bucket}]` does not name a SHA bucket (known buckets: {known})")
    # A typo in `files` is otherwise silent: `explain` drops the `--files`
    # suffix, the runner falls back to its whole discovered set, and CI goes
    # green having tested the wrong thing.
    for name, runner in cfg.runners.items():
        if runner.files and runner.files not in cfg.file_sets:
            known = ", ".join(sorted(cfg.file_sets)) or "(none)"
            raise ConfigError(
                f"{path}: `[runners.{name}].files` names no `files = true` table: "
                f"{runner.files} (known file sets: {known})"
            )
    for name, gate in cfg.gates.items():
        _check_gate(cfg, path, name, gate)
    _warn_unhashable_sha_globs(cfg, path)


def _check_gate(cfg: Config, path: str, name: str, gate: GateSpec) -> None:
    """Reject a scope entry or a placeholder that names nothing.

    Both are otherwise silent: an unknown scope entry hashes no content, and an
    unknown placeholder reaches the command as a literal argument.
    """
    known_buckets = set(cfg.sha_bucket.values())
    for entry in gate.scope:
        if entry in known_buckets:
            continue
        if entry in cfg.file_sets:
            raise ConfigError(
                f"{path}: `[gate.{name}].scope` entry `{entry}` is a `files = true` table, "
                "which is a projection and has no content hash"
            )
        if entry not in cfg.paths:
            raise ConfigError(f"{path}: `[gate.{name}].scope` entry `{entry}` names no bucket or tag")
        if not cfg.paths[entry]:
            raise ConfigError(f"{path}: `[gate.{name}].scope` entry `{entry}` is a tag that has no `paths`")
    # An error here, where a SHA bucket only warns: for an image the cost is a
    # stale tag, for a gate it is a false pass.
    for tag, glob in _unhashable_globs(cfg, scope_tags(cfg, gate)):
        raise ConfigError(
            f"{path}: `[{tag}].paths` glob `{glob}` is not a literal path or `dir/**`, so it contributes "
            f"nothing to the key of `[gate.{name}]`, whose scope it feeds"
        )
    for argv in gate.commands:
        for token in argv:
            if "{" not in token and "}" not in token:
                continue
            inner = token[1:-1]
            if not is_placeholder(token) or "{" in inner or "}" in inner:
                raise ConfigError(
                    f"{path}: `[gate.{name}] cmd` argument `{token}`: a `{{...}}` placeholder must be a whole argument"
                )
            if inner.endswith(ITEMS_PLACEHOLDER_SUFFIX):
                if inner[: -len(ITEMS_PLACEHOLDER_SUFFIX)] not in cfg.items:
                    known = ", ".join(sorted(cfg.items)) or "(none)"
                    raise ConfigError(
                        f"{path}: `[gate.{name}] cmd` placeholder `{token}` names no `*_items` list (known: {known})"
                    )
            elif inner not in cfg.file_sets:
                known = ", ".join(sorted(cfg.file_sets)) or "(none)"
                raise ConfigError(
                    f"{path}: `[gate.{name}] cmd` placeholder `{token}` names no `files = true` table (known: {known})"
                )


def is_placeholder(token: str) -> bool:
    """Whether a command argument is a `{...}` placeholder."""
    return token.startswith("{") and token.endswith("}")


def scope_tags(cfg: Config, gate: GateSpec) -> list[str]:
    """The tags a gate's scope resolves to: a bucket's members, or the tag itself.

    A bucket name wins over a tag of the same name, so `sha = true` on a tag
    brings in every tag that contributes to that bucket.
    """
    return sorted({tag for entry in gate.scope for tag in entry_tags(cfg, entry)})


def entry_tags(cfg: Config, entry: str) -> list[str]:
    """The tags one scope entry resolves to."""
    members = [tag for tag, bucket in cfg.sha_bucket.items() if bucket == entry]
    return sorted(members) or [entry]


def _warn_unhashable_sha_globs(cfg: Config, path: str) -> None:
    """Warn about globs that contribute nothing to a bucket's content hash.

    `git ls-tree` takes literal path prefixes, not globs, so a tag whose `paths`
    is `src/*.py` matches files for tag resolution but contributes an empty walk
    to any SHA bucket it feeds — the bucket's hash then stops moving when the
    source changes, and the image silently stops rebuilding. Only a literal path
    or a `dir/**` prefix survives the reduction.
    """
    for tag, glob in _unhashable_globs(cfg, cfg.sha_bucket):
        print(
            f"{path}: warning: `[{tag}].paths` glob `{glob}` is not a literal path or `dir/**`, "
            "so it contributes nothing to the SHA bucket it feeds",
            file=sys.stderr,
        )


def _unhashable_globs(cfg: Config, tags: Iterable[str]) -> list[tuple[str, str]]:
    """(tag, glob) for each glob `git ls-tree` cannot walk, in `tags` and their transitive deps."""
    contributing: set[str] = set()
    for tag in tags:
        contributing.add(tag)
        contributing |= _forward_deps(tag, cfg.depends)
    out: list[tuple[str, str]] = []
    for tag in sorted(contributing):
        for glob in cfg.paths.get(tag, []):
            stripped = glob[: -len("/**")] if glob.endswith("/**") else glob
            if any(ch in stripped for ch in "*?["):
                out.append((tag, glob))
    return out


def _forward_deps(tag: str, depends: dict[str, list[str]]) -> set[str]:
    """Transitive closure of what `tag` depends on."""
    seen: set[str] = set()
    queue = [tag]
    while queue:
        cur = queue.pop()
        for dep in depends.get(cur, ()):
            if dep not in seen:
                seen.add(dep)
                queue.append(dep)
    return seen


def version_var_for(bucket: str) -> str:
    """The environment variable name carrying a bucket's version.

    `astronort-lector` -> `ASTRONORT_LECTOR_VERSION`. This derivation is what
    `deploy`'s `export $(tangier changemap sha --all)` relies on, and what k8s
    manifests reference — it is a contract, not an implementation detail.
    """
    return f"{bucket.upper().replace('-', '_')}_VERSION"


def read_config(path: str) -> Config:
    """Parse `pipeline.toml`.

    Three phases: partition top-level keys into reserved sections and tags;
    merge bare and `[tags.*]` tag tables into one flat map and validate each;
    then post-parse checks (dependency targets, cycles, warnings) that need the
    whole tag set — which is what lets `depends` refer forward across the
    bare/nested boundary.
    """
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    cfg = Config()

    # --- A. Partition -----------------------------------------------------
    bare_tags: dict[str, Any] = {}
    nested_tags: dict[str, Any] = {}
    for key, body in raw.items():
        if key not in RESERVED_SECTIONS:
            bare_tags[key] = body
            continue
        table = _require_table(path, key, body)
        if key == "tags":
            # `[tags]`' keys are tag NAMES, so a field written directly under it
            # would be read as a tag. Say that, rather than reporting the field
            # name as a malformed tag.
            for name, value in table.items():
                if not isinstance(value, dict):
                    raise ConfigError(
                        f"{path}: `[tags].{name}` must be a table — `[tags]` holds tag names, "
                        f"so write `[tags.{name}]` with its fields underneath"
                    )
            nested_tags = table
        elif key == "sha":
            cfg.sha = _parse_sha(path, table)
        elif key == "registry":
            cfg.registry = _parse_registry(path, table)
        elif key == "runners":
            cfg.runners = _parse_runners(path, table)
        elif key == "image":
            cfg.images = _parse_images(path, table)
        elif key == "k8s":
            cfg.k8s = _parse_k8s(path, table)
        elif key == "deploy":
            cfg.deploy_envs, cfg.rollout, cfg.after = _parse_deploy(path, table)
        elif key == "tailnet":
            cfg.tailnet = _parse_tailnet(path, table)
        elif key == "gate":
            cfg.gates = _parse_gates(path, table)

    # --- B. Merge and validate tags ---------------------------------------
    merged: dict[str, Any] = dict(bare_tags)
    for name, body in nested_tags.items():
        if name in merged:
            raise ConfigError(f"{path}: tag `{name}` is declared both as `[{name}]` and `[tags.{name}]`")
        merged[name] = body

    raw_depends: dict[str, list[str]] = {}
    for tag, body in merged.items():
        if not isinstance(body, dict):
            raise ConfigError(f"{path}: top-level `{tag}` must be a table, got {type(body).__name__}")
        _parse_tag(cfg, path, tag, body, raw_depends)

    # --- C. Post-parse ----------------------------------------------------
    for tag, deps in raw_depends.items():
        for dep in deps:
            if dep not in cfg.paths:
                raise ConfigError(f"{path}: `[{tag}].depends` references unknown tag `{dep}`")
    cfg.depends = raw_depends
    _check_no_cycles(cfg.depends, path)
    _derive(cfg, path)
    _warn_tags_without_input(cfg, path)
    _warn_tags_without_output(cfg, path)
    _warn_tailnet_envs_without_deploy(cfg, path)
    return cfg
