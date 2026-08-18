"""Content-addressed image tagging, building and compose rendering.

`build_argv` is **pure** — no Runner, no I/O, no environment lookups beyond the
secret-name check it is handed. That is what lets the buildx invocation be
asserted as a rendered command line rather than executed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from tangier.changemap import buckets, sha_for_bucket
from tangier.config import Config, ImageSpec


class ImageError(RuntimeError):
    """A problem with an image operation that should exit non-zero."""


def image_ref(cfg: Config, bucket: str) -> str:
    """The registry path for a bucket's image, without a tag."""
    if not cfg.registry:
        raise ImageError("no `[registry] url` in config")
    return f"{cfg.registry}/{bucket}"


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


def build_argv(
    spec: ImageSpec,
    ref: str,
    tag: str,
    *,
    push: bool = False,
    load: bool = False,
    secrets: list[SecretArg] | None = None,
) -> list[str]:
    """Render the `docker buildx build` command line.

    Argument order reproduces bin/build-github exactly. Kept verbatim from it:

    - the whole cache-to string. `mode=max` keeps builder-stage layers, and
      `image-manifest=true,oci-mediatypes=true` is required for a plain
      `distribution` registry to accept the cache manifest — non-obvious, not
      guessable, and its absence makes the cache export fail outright.
    - the unconditional `--build-arg PACKAGE_VERSION=<tag>`, which every
      Dockerfile turns into its Sentry release identifier.
    - also tagging `:latest` when pushing a non-latest tag.
    - both `--output` forms: `type=registry` pushes, `type=image,name=...` does not.

    The build context is always the repo root. `code-path` in the old workflows
    was a *dockerfile prefix*, not a context — modelling it as a context breaks
    every build.
    """
    argv = ["docker", "buildx", "build"]

    if load:
        # The local dev-loop form: cross-build and load into the local daemon so
        # docker compose can use the image.
        if spec.platform:
            argv += ["--platform", spec.platform]
        argv += ["--load"]
    elif push and spec.cache:
        cache_ref = f"{ref}:buildcache"
        argv += [
            "--cache-from",
            f"type=registry,ref={cache_ref}",
            "--cache-to",
            f"type=registry,ref={cache_ref},mode=max,image-manifest=true,oci-mediatypes=true",
        ]

    for secret in secrets or []:
        argv += ["--secret", f"id={secret.id},env={secret.env}"]

    argv += ["--file", spec.dockerfile, "--tag", f"{ref}:{tag}"]
    if push and tag != "latest":
        argv += ["--tag", f"{ref}:latest"]

    if not load:
        argv += ["--output", "type=registry" if push else f"type=image,name={ref}"]

    argv += ["--build-arg", f"PACKAGE_VERSION={tag}"]
    for key in sorted(spec.args):
        argv += ["--build-arg", f"{key}={spec.args[key]}"]

    argv.append(spec.context)
    return argv


def exists_argv(ref: str, tag: str, platform: str = "linux/amd64") -> list[str]:
    """The `regctl` probe for whether a tag is already published."""
    return ["regctl", "manifest", "get", "--platform", platform, f"{ref}:{tag}"]


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
