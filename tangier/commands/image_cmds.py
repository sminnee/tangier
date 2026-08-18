"""`tangier image ...` — content-addressed tagging, publishing and compose rendering."""

from __future__ import annotations

import argparse

from tangier.config import Config
from tangier.github import emit_outputs
from tangier.image import (
    ImageError,
    build_argv,
    exists_argv,
    image_ref,
    render_compose,
    resolve_secrets,
    tag_for,
)
from tangier.runner import Runner, Subprocess


def _runner(args: argparse.Namespace) -> Runner:
    return getattr(args, "runner", None) or Subprocess(echo=True)


def cmd_tag(config: Config, args: argparse.Namespace) -> int:
    print(tag_for(config, args.bucket, args.head))
    return 0


def published(config: Config, runner: Runner, bucket: str, tag: str) -> bool:
    """Whether `<bucket>:<tag>` is already in the registry.

    A missing `regctl` raises rather than reporting "missing". The old script
    printed `missing` when regctl was absent, which caused a rebuild-and-push of
    an already-published tag — a fail-open that costs a full build and moves
    `:latest` for no reason.
    """
    if runner.which("regctl") is None:
        raise ImageError("regctl not found on PATH — cannot check whether the tag is published")
    ref = image_ref(config, bucket)
    return runner.run(exists_argv(ref, tag)).ok


def cmd_exists(config: Config, args: argparse.Namespace) -> int:
    """Print `exists`/`missing` AND set the exit code.

    Both contracts on purpose: existing CI captures the string and compares it,
    so migration is a path swap; new callers can branch on the exit code.
    """
    tag = args.tag or tag_for(config, args.bucket, args.head)
    if published(config, _runner(args), args.bucket, tag):
        print("exists")
        return 0
    print("missing")
    return 1


def cmd_build(config: Config, args: argparse.Namespace) -> int:
    spec = config.images.get(args.bucket)
    if spec is None:
        known = ", ".join(sorted(config.images)) or "(none)"
        raise ImageError(f"no `[image.{args.bucket}]` in config — not buildable here (configured: {known})")

    runner = _runner(args)
    tag = args.tag or tag_for(config, args.bucket, args.head)
    ref = image_ref(config, args.bucket)

    extra_secrets = [_normalise_secret(s) for s in (args.secret or [])]
    secrets = resolve_secrets(spec.secrets, extra_secrets)
    argv = build_argv(spec, ref, tag, push=args.push, load=args.load, secrets=secrets)

    # `--print` is a dry run, so it must not need a registry: probing first
    # would make it fail on exactly the machine most likely to want it. It
    # therefore emits no outputs either — there is no build to describe.
    if args.print:
        print(" ".join(argv))
        return 0

    # `emit_outputs` echoes to stdout, so from here on `image build`'s stdout
    # carries `tag=`/`built=` lines. Safe: only `image exists` has a
    # captured-string contract (see `cmd_exists`), and nothing captures this.
    #
    # `built`, not `published`: `image exists` already owns "published", and
    # that word would be true both when we skipped and when we built.
    #
    # Skip when the tag is already published: this idempotence is the whole
    # point of content-addressed tags, and losing it rebuilds every image.
    if args.push and not args.force and published(config, runner, args.bucket, tag):
        print(f"{ref}:{tag} already published; skipping build")
        emit_outputs({"tag": tag, "built": "false"})
        return 0

    result = runner.run(argv, capture=False)
    # A failed build still emits, with `built=false`; the step fails on the
    # returncode, and a downstream `if:` reading `built` sees the truth.
    emit_outputs({"tag": tag, "built": "true" if result.ok else "false"})
    return result.returncode


def cmd_compose(config: Config, args: argparse.Namespace) -> int:
    with open(args.template) as fh:
        template = fh.read()
    print(render_compose(config, template, args.head))
    return 0


def add_parsers(sub: argparse._SubParsersAction) -> None:
    img = sub.add_parser("image", help="content-addressed image tags and builds")
    isub = img.add_subparsers(dest="cmd", required=True)

    tp = isub.add_parser("tag", help="print a bucket's content-hash tag")
    _ = tp.add_argument("bucket")
    _ = tp.add_argument("--head", default="HEAD")
    tp.set_defaults(func=cmd_tag)

    ep = isub.add_parser("exists", help="is this tag already published? prints exists/missing, exits 0/1")
    _ = ep.add_argument("bucket")
    _ = ep.add_argument("--tag", default=None, help="tag to probe (default: the computed content hash)")
    _ = ep.add_argument("--head", default="HEAD")
    ep.set_defaults(func=cmd_exists)

    bp = isub.add_parser("build", help="build a bucket's image, skipping when already published")
    _ = bp.add_argument("bucket")
    _ = bp.add_argument("--tag", default=None)
    _ = bp.add_argument("--head", default="HEAD")
    # Mutually exclusive: --load takes the local-daemon path, which emits no
    # `--output type=registry`, so combining them would report success while
    # pushing nothing.
    output = bp.add_mutually_exclusive_group()
    _ = output.add_argument(
        "--push",
        action="store_true",
        help="push to the registry, enable the registry cache, and also tag :latest",
    )
    _ = output.add_argument("--load", action="store_true", help="build for the local docker daemon (dev loop)")
    _ = bp.add_argument("--force", action="store_true", help="build even when the tag is already published")
    _ = bp.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="id=<id>",
        help="extra buildx secret id; emitted only when its env var is set (repeatable)",
    )
    _ = bp.add_argument("--print", action="store_true", help="print the command line instead of running it")
    bp.set_defaults(func=cmd_build)

    cp = isub.add_parser("compose", help="render [[bucket]] placeholders in a compose template")
    _ = cp.add_argument("template")
    _ = cp.add_argument("--head", default="HEAD")
    cp.set_defaults(func=cmd_compose)


def _normalise_secret(value: str) -> str:
    """Accept both `--secret id=x` and the bare `--secret x`."""
    return value[len("id=") :] if value.startswith("id=") else value
