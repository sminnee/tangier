"""Content-addressed image tagging, building and compose rendering.

`build_argv` is **pure** — no Runner, no I/O, no environment lookups beyond the
secret-name check it is handed. That is what lets the buildx invocation be
asserted as a rendered command line rather than executed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Literal

from tangier.changemap import buckets, sha_for_bucket
from tangier.config import Config, ImageSpec


class ImageError(RuntimeError):
    """A problem with an image operation that should exit non-zero."""


def image_ref(cfg: Config, bucket: str) -> str:
    """The registry path for a bucket's image, without a tag."""
    if not cfg.registry:
        raise ImageError("no `[registry] url` in config")
    return f"{cfg.registry}/{bucket}"


def destination_refs(cfg: Config, bucket: str, *, mirrors: bool = True) -> list[str]:
    """Every registry path a pushed image goes to: the primary first, then each mirror.

    Only the primary is ever read from. A mirror is a write-only copy.
    """
    primary = image_ref(cfg, bucket)
    if not mirrors:
        return [primary]
    return [primary, *(f"{mirror}/{bucket}" for mirror in cfg.registry_mirrors)]


def tag_for(cfg: Config, bucket: str, head: str = "HEAD") -> str:
    """The content hash that tags this bucket's image."""
    if bucket not in buckets(cfg):
        known = ", ".join(sorted(buckets(cfg))) or "(none)"
        raise ImageError(f"unknown bucket: {bucket} (known: {known})")
    return sha_for_bucket(cfg, bucket, head)


def parse_image_tag(image: str) -> str:
    """The tag from a full image reference.

    `awk -F: '{print $NF}'` returns `5000` for `registry:5000/ns/img` and the
    digest for a pinned image, after which a deploy deletes a nonsense Job and
    rolls back against garbage. The registry is one config edit away from having
    a port, so the split is done properly: only a colon in the final path
    segment introduces a tag, and a `@digest` is stripped first.
    """
    ref = image.split("@", 1)[0]
    last_slash = ref.rfind("/")
    last_colon = ref.rfind(":")
    if last_colon > last_slash:
        return ref[last_colon + 1 :]
    return ""


@dataclass
class SecretArg:
    """A buildx `--secret id=<id>,env=<ENV>` pair."""

    id: str
    env: str

    @classmethod
    def from_id(cls, secret_id: str) -> SecretArg:
        # build-github's convention: id `sentry_auth_token` reads the
        # `SENTRY_AUTH_TOKEN` environment variable.
        return cls(id=secret_id, env=secret_id.upper())


def resolve_secrets(
    spec_secrets: list[str],
    extra: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> list[SecretArg]:
    """Union config and CLI secrets, keeping only those present in the environment.

    Preserves build-github's guard: a secret whose variable is unset is silently
    omitted rather than passed empty. The value never enters Python — only the
    variable name reaches the command line.
    """
    environ = os.environ if env is None else env
    seen: set[str] = set()
    out: list[SecretArg] = []
    for secret_id in [*spec_secrets, *(extra or [])]:
        if secret_id in seen:
            continue
        seen.add(secret_id)
        arg = SecretArg.from_id(secret_id)
        if environ.get(arg.env):
            out.append(arg)
    return out


def platform_suffix(platform: str) -> str:
    """The tag suffix for a platform: `linux/arm64` gives `linux-arm64`."""
    return platform.replace("/", "-")


def leg_tag(tag: str, platform: str) -> str:
    """The tag one platform's leg pushes before `image merge` joins the legs."""
    return f"{tag}-{platform_suffix(platform)}"


def build_argv(
    spec: ImageSpec,
    ref: str,
    tag: str,
    *,
    push: bool = False,
    secrets: list[SecretArg] | None = None,
    mirrors: list[str] | None = None,
    leg: str | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    """Render the `docker buildx build` command line: a push, or a local build into the Docker daemon.

    The push form's argument order reproduces bin/build-github exactly. Kept verbatim from it:

    - the whole cache-to string. `mode=max` keeps builder-stage layers, and
      `image-manifest=true,oci-mediatypes=true` is required for a plain
      `distribution` registry to accept the cache manifest — non-obvious, not
      guessable, and its absence makes the cache export fail outright.
    - the unconditional `--build-arg PACKAGE_VERSION=<tag>`, which every
      Dockerfile turns into its Sentry release identifier.
    - also tagging `:latest` when pushing a non-latest tag.

    A push also tags each of `mirrors` (`:tag` and `:latest`), so one buildx run
    publishes to every registry. The cache lives on `ref`, the primary, only.

    A local build reads the cache but never writes it: see "Images" in
    docs/guide/4-packages-and-builds.md for why.

    `leg` is the platform of one leg of a multi-platform image. It builds that
    platform, tags only `<ref>:<tag>-<os>-<arch>` and caches to
    `:buildcache-<os>-<arch>`: see "Multi-platform images" in
    docs/guide/4-packages-and-builds.md for why.

    `extra` is passed to buildx verbatim, just before the context.

    The build context is always the repo root. `code-path` in the old workflows
    was a *dockerfile prefix*, not a context — modelling it as a context breaks
    every build.
    """
    argv = ["docker", "buildx", "build"]
    cache_ref = f"{ref}:buildcache-{platform_suffix(leg)}" if leg else f"{ref}:buildcache"

    if not push:
        # Cross-build and load into the local daemon so docker compose can use the image.
        if spec.platform:
            argv += ["--platform", spec.platform]
        argv += ["--load"]
        if spec.cache:
            argv += ["--cache-from", f"type=registry,ref={cache_ref}"]
    else:
        if leg:
            # The tag names this platform, so make the builder build exactly it.
            argv += ["--platform", leg]
        if spec.cache:
            argv += [
                "--cache-from",
                f"type=registry,ref={cache_ref}",
                "--cache-to",
                f"type=registry,ref={cache_ref},mode=max,image-manifest=true,oci-mediatypes=true",
            ]

    for secret in secrets or []:
        argv += ["--secret", f"id={secret.id},env={secret.env}"]

    argv += ["--file", spec.dockerfile]
    if push and leg:
        argv += ["--tag", f"{ref}:{leg_tag(tag, leg)}"]
    else:
        for dest in [ref, *(mirrors or [])] if push else [ref]:
            argv += ["--tag", f"{dest}:{tag}"]
            if push and tag != "latest":
                argv += ["--tag", f"{dest}:latest"]

    if push:
        argv += ["--output", "type=registry"]

    argv += ["--build-arg", f"PACKAGE_VERSION={tag}"]
    for key in sorted(spec.args):
        argv += ["--build-arg", f"{key}={spec.args[key]}"]

    argv += extra or []
    argv.append(spec.context)
    return argv


@dataclass
class BuildStep:
    """What `image build` does with one bucket: `build` it, or `reuse` its published tag."""

    bucket: str
    tag: str
    action: Literal["build", "reuse"]


def plan_build(
    cfg: Config,
    bucket_names: list[str],
    head: str = "HEAD",
    *,
    force: bool = False,
    dirty: Collection[str] = (),
    is_published: Callable[[str, str], bool],
    tag: str | None = None,
) -> list[BuildStep]:
    """Decide, per bucket, whether to build it or reuse its published tag.

    A bucket is reused only when its tag is published, it is not in `dirty`,
    and `force` is off; `is_published(bucket, tag)` is not called otherwise.
    Every check that can raise runs before the caller pulls or builds anything.
    `tag` overrides the computed tag.
    """
    known = buckets(cfg)
    for name in bucket_names:
        if name not in known:
            raise ImageError(f"unknown bucket: {name} (known: {', '.join(sorted(known)) or '(none)'})")

    steps: list[BuildStep] = []
    for name in bucket_names:
        step_tag = tag or sha_for_bucket(cfg, name, head)
        reuse = not force and name not in dirty and is_published(name, step_tag)
        steps.append(BuildStep(name, step_tag, "reuse" if reuse else "build"))

    configured = ", ".join(sorted(cfg.images)) or "(none)"
    for step in steps:
        if step.action == "build" and step.bucket not in cfg.images:
            raise ImageError(f"no `[image.{step.bucket}]` in config — not buildable here (configured: {configured})")
    return steps


def exists_argv(ref: str, tag: str, platform: str = "linux/amd64") -> list[str]:
    """The `regctl` probe for whether a tag is already published."""
    return ["regctl", "manifest", "get", "--platform", platform, f"{ref}:{tag}"]


def manifest_argv(ref: str, tag: str) -> list[str]:
    """The `regctl` fetch of a tag's top-level manifest, as JSON, to read its platforms.

    Not `exists_argv`: `regctl manifest get --platform` ignores the platform
    when the tag is a single manifest, not an index, and succeeds for any.
    """
    return ["regctl", "manifest", "get", "--format", "raw-body", f"{ref}:{tag}"]


def config_platform_argv(ref: str, tag: str) -> list[str]:
    """The `regctl` lookup of a single-manifest image's platform, from its config."""
    fmt = "{{.OS}}/{{.Architecture}}{{if .Variant}}/{{.Variant}}{{end}}"
    return ["regctl", "image", "config", "--format", fmt, f"{ref}:{tag}"]


def index_platforms(body: str) -> list[str] | None:
    """The platforms an index lists, or None when `body` is a single manifest.

    Attestation entries (`unknown/unknown`) are left out.
    """
    try:
        manifest = json.loads(body)
    except ValueError as e:
        raise ImageError(f"registry returned a manifest that is not JSON: {e}") from e
    if "manifests" not in manifest:
        return None
    found: list[str] = []
    for entry in manifest["manifests"]:
        p = entry.get("platform") or {}
        if p.get("os", "unknown") == "unknown":
            continue
        found.append("/".join(x for x in (p.get("os"), p.get("architecture"), p.get("variant")) if x))
    return found


def has_platform(platforms: Collection[str], want: str) -> bool:
    """Whether `want` (`os/arch[/variant]`) is among `platforms`.

    A variant is compared only when both sides name one, so `linux/arm64`
    matches the `linux/arm64/v8` an arm64 image's config reports.
    """
    w = want.split("/")
    for have in platforms:
        h = have.split("/")
        if h[:2] == w[:2] and (len(h) < 3 or len(w) < 3 or h[2] == w[2]):
            return True
    return False


def native_platform_argv() -> list[str]:
    """The Docker daemon's own platform, such as `linux/arm64` on an arm64 runner."""
    return ["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"]


def digest_argv(ref: str, tag: str) -> list[str]:
    """The `regctl` lookup of a tag's top-level manifest digest.

    No `--platform`, unlike `exists_argv`: for a multi-platform image this is
    the index digest, so the kubelet still picks its own platform.
    """
    return ["regctl", "image", "digest", f"{ref}:{tag}"]


def copy_argv(src: str, dst: str) -> list[str]:
    """The `regctl` copy of a published image (every platform) to another registry."""
    return ["regctl", "image", "copy", src, dst]


def render_compose(cfg: Config, template: str, head: str = "HEAD") -> str:
    """Substitute `[[bucket]]` placeholders with fully-qualified image refs.

    Unmatched `[[foo]]` is left as-is, and `${VAR}` is left for docker-compose
    to expand. A bucket with no placeholder is simply a no-op replace.
    """
    out = template
    for bucket in sorted(buckets(cfg)):
        version = sha_for_bucket(cfg, bucket, head)
        out = out.replace(f"[[{bucket}]]", f"{image_ref(cfg, bucket)}:{version}")
    return out
