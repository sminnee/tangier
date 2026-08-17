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

import sys
import tomllib
from dataclasses import dataclass, field
from typing import Any

# Top-level names that configure tangier rather than declaring a tag. Reserved
# now even where the behaviour lands later: reserving a name is cheap,
# un-reserving one after a config author used it as a tag is a breaking change.
RESERVED_SECTIONS = frozenset({"tags", "sha", "registry", "deploy", "image", "k8s", "runners"})

# Recognised tag-table fields. `_items` is matched by suffix.
_KNOWN_FIELDS = {"paths", "exclude", "depends", "sha", "touched", "files"}

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


def _parse_deploy(path: str, body: dict[str, Any]) -> tuple[dict[str, DeployEnv], RolloutSettings]:
    envs: dict[str, DeployEnv] = {}
    rollout = RolloutSettings()
    for name, spec in body.items():
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
    return envs, rollout


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
    for tag in cfg.paths:
        has_output = tag in cfg.sha_bucket or tag in cfg.touched or tag in items_tags or tag in depended_upon
        if not has_output:
            print(
                f"{path}: warning: `[{tag}]` has no sha, touched, *_items, and nothing depends on it — no CI signal",
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
    _warn_unhashable_sha_globs(cfg, path)


def _warn_unhashable_sha_globs(cfg: Config, path: str) -> None:
    """Warn about globs that contribute nothing to a bucket's content hash.

    `git ls-tree` takes literal path prefixes, not globs, so a tag whose `paths`
    is `src/*.py` matches files for tag resolution but contributes an empty walk
    to any SHA bucket it feeds — the bucket's hash then stops moving when the
    source changes, and the image silently stops rebuilding. Only a literal path
    or a `dir/**` prefix survives the reduction.
    """
    contributing: set[str] = set()
    for tag, bucket in cfg.sha_bucket.items():
        del bucket
        contributing.add(tag)
        contributing |= _forward_deps(tag, cfg.depends)
    for tag in sorted(contributing):
        for glob in cfg.paths.get(tag, []):
            stripped = glob[: -len("/**")] if glob.endswith("/**") else glob
            if any(ch in stripped for ch in "*?["):
                print(
                    f"{path}: warning: `[{tag}].paths` glob `{glob}` is not a literal path or `dir/**`, "
                    "so it contributes nothing to the SHA bucket it feeds",
                    file=sys.stderr,
                )


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
            cfg.deploy_envs, cfg.rollout = _parse_deploy(path, table)

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
    return cfg
