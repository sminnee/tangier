"""`tangier image ...` — content-addressed tagging, publishing and compose rendering."""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from tangier import git
from tangier.changemap import buckets, scope_touched
from tangier.config import Config
from tangier.github import emit_outputs
from tangier.image import (
    BuildStep,
    ImageError,
    build_argv,
    copy_argv,
    destination_refs,
    exists_argv,
    image_ref,
    plan_build,
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


def published(runner: Runner, ref: str, tag: str) -> bool:
    """Whether `<ref>:<tag>` is already in its registry.

    A missing `regctl` raises rather than reporting "missing". The old script
    printed `missing` when regctl was absent, which caused a rebuild-and-push of
    an already-published tag — a fail-open that costs a full build and moves
    `:latest` for no reason.
    """
    if runner.which("regctl") is None:
        raise ImageError("regctl not found on PATH — cannot check whether the tag is published")
    return runner.run(exists_argv(ref, tag)).ok


def cmd_exists(config: Config, args: argparse.Namespace) -> int:
    """Print `exists`/`missing` AND set the exit code.

    Both contracts on purpose: existing CI captures the string and compares it,
    so migration is a path swap; new callers can branch on the exit code.
    """
    tag = args.tag or tag_for(config, args.bucket, args.head)
    if published(_runner(args), image_ref(config, args.bucket), tag):
        print("exists")
        return 0
    print("missing")
    return 1


def cmd_build(config: Config, args: argparse.Namespace) -> int:
    """Get each bucket's image to the registry (`--push`) or the local daemon (`--pull`).

    A bucket whose tag is already published is reused unless `--force`: `--push`
    skips it, or copies it to a destination that lacks it, and `--pull` runs
    `docker pull`. Every other bucket is built, up to `--jobs` at a time.
    """
    names: list[str] = list(dict.fromkeys(args.buckets))
    if args.tag and len(names) > 1:
        raise ImageError("--tag names one image, so it takes one bucket")
    push = not args.pull
    runner = _runner(args)
    extra_secrets = [_normalise_secret(s) for s in (args.secret or [])]
    # Mirrors are push destinations only: a pull reads `url`.
    with_mirrors = push and not args.no_mirrors

    def build_cmd(bucket: str, tag: str) -> list[str]:
        spec = config.images[bucket]
        refs = destination_refs(config, bucket, mirrors=with_mirrors)
        secrets = resolve_secrets(spec.secrets, extra_secrets)
        return build_argv(spec, refs[0], tag, push=push, secrets=secrets, mirrors=refs[1:])

    # `--print` is a dry run, so it must not need a registry: probing first
    # would make it fail on exactly the machine most likely to want it. It
    # therefore emits no outputs either — there is no build to describe.
    if args.print:
        steps = plan_build(config, names, args.head, force=True, is_published=lambda b, t: False, tag=args.tag)
        for step in steps:
            print(" ".join(build_cmd(step.bucket, step.tag)))
        return 0

    dirty = _dirty_buckets(config, names, args.head)
    if push and dirty:
        detail = "; ".join(f"{bucket}: {', '.join(files)}" for bucket, files in dirty.items())
        raise ImageError(
            "uncommitted changes would be pushed under a tag that hashes the committed tree. "
            f"Commit or remove them first ({detail})"
        )

    # Which destinations already hold each bucket's tag, from the `--push` probe.
    have: dict[str, list[str]] = {}

    def is_published(bucket: str, tag: str) -> bool:
        if not push:
            return published(runner, image_ref(config, bucket), tag)
        refs = destination_refs(config, bucket, mirrors=with_mirrors)
        have[bucket] = [r for r in refs if published(runner, r, tag)]
        return bool(have[bucket])

    steps = plan_build(config, names, args.head, force=args.force, dirty=dirty, is_published=is_published, tag=args.tag)

    # A missing docker would raise inside a build thread and lose the other builds' output.
    if runner.which("docker") is None and (not push or any(s.action == "build" for s in steps)):
        raise ImageError("docker not found on PATH — cannot pull or build")

    # bucket -> (what happened, its exit code). The word is a noun so `failed` can follow it.
    outcome: dict[str, tuple[str, int]] = {}
    for step in steps:
        if step.action != "reuse":
            continue
        if push:
            refs = destination_refs(config, step.bucket, mirrors=with_mirrors)
            missing = [r for r in refs if r not in have[step.bucket]]
            rc = _fill_from(runner, have[step.bucket][0], missing, step.tag)
            outcome[step.bucket] = ("copy" if missing else "skip", rc)
        else:
            outcome[step.bucket] = ("pull", runner.run(_pull_argv(config, step), capture=False).returncode)

    builds = {s.bucket: build_cmd(s.bucket, s.tag) for s in steps if s.action == "build"}
    for bucket, code in _run_builds(runner, builds, args.jobs).items():
        outcome[bucket] = ("build", code)

    done = {"build": "built", "pull": "pulled", "copy": "copied", "skip": "skipped"}
    rc = 0
    for step in steps:
        action, code = outcome[step.bucket]
        note = f" (uncommitted changes: {', '.join(dirty[step.bucket])})" if step.bucket in dirty else ""
        if code != 0:
            rc = rc or code
        print(f"{done[action] if code == 0 else action + ' failed'} {step.bucket}{note}")

    # One bucket emits `tag=`/`built=` for the build action; with several, no one tag fits.
    # `built`, not `published`: `image exists` owns "published", which is true whether we
    # skipped or built. A failed build emits `built=false` and fails on the returncode.
    if len(steps) == 1:
        [step] = steps
        built = outcome[step.bucket] == ("build", 0)
        emit_outputs({"tag": step.tag, "built": "true" if built else "false"})
    return rc


def _pull_argv(config: Config, step: BuildStep) -> list[str]:
    """`docker pull` of a reused bucket, for the platform a local build of it would target."""
    spec = config.images.get(step.bucket)
    platform = ["--platform", spec.platform] if spec and spec.platform else []
    return ["docker", "pull", *platform, f"{image_ref(config, step.bucket)}:{step.tag}"]


def _run_builds(runner: Runner, builds: dict[str, list[str]], jobs: int) -> dict[str, int]:
    """Run each bucket's build command, `jobs` at a time, and return each exit code.

    One build streams its output live. Builds side by side would interleave
    theirs, so each is captured and printed whole to stderr when it ends.
    """
    capture = jobs > 1 and len(builds) > 1
    codes: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(runner.run, argv, capture=capture): bucket for bucket, argv in builds.items()}
        for future in as_completed(futures):
            bucket, result = futures[future], future.result()
            if capture:
                print(f"== build {bucket} (exit {result.returncode}) ==", file=sys.stderr)
                sys.stderr.write(result.stdout + result.stderr)
                sys.stderr.flush()
            codes[bucket] = result.returncode
    return codes


def _dirty_buckets(config: Config, names: list[str], head: str) -> dict[str, list[str]]:
    """Each named bucket with uncommitted changes, and the files that make it so.

    The same globs and filters as the bucket's hash, so a file counts exactly
    when committing it would change the tag.
    """
    files = git.uncommitted_files(head)
    members = buckets(config)
    dirty: dict[str, list[str]] = {}
    for name in names:
        touching = [f for f in files if name in members and scope_touched(config, members[name], [f])]
        if touching:
            dirty[name] = touching
    return dirty


def _fill_from(runner: Runner, src: str, missing: list[str], tag: str) -> int:
    """Copy an already-published tag to each destination that lacks it, without a build.

    This fills a mirror added after the tag was published, and catches up a
    destination whose push failed. `:latest` stays where it is, as on a skip.
    """
    if not missing:
        print(f"{src}:{tag} already published; skipping build")
    for dst in missing:
        print(f"{src}:{tag} already published; copying to {dst}")
        result = runner.run(copy_argv(f"{src}:{tag}", f"{dst}:{tag}"), capture=False)
        if not result.ok:
            return result.returncode
    return 0


def cmd_registries(config: Config, args: argparse.Namespace) -> int:
    """Print each registry `image build --push` pushes to, one per line: `url` first.

    The build action reads this to decide which logins it needs, so it follows
    the same config the build does.
    """
    if not config.registry:
        raise ImageError("no `[registry] url` in config")
    print(config.registry)
    if not args.no_mirrors:
        for mirror in config.registry_mirrors:
            print(mirror)
    return 0


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

    bp = isub.add_parser(
        "build",
        help="get bucket images to the registry (--push) or the local daemon (--pull), reusing published tags",
    )
    _ = bp.add_argument("buckets", nargs="+", metavar="bucket")
    _ = bp.add_argument(
        "--tag", default=None, help="tag to build (default: the computed content hash); one bucket only"
    )
    _ = bp.add_argument("--head", default="HEAD")
    output = bp.add_mutually_exclusive_group(required=True)
    _ = output.add_argument(
        "--push",
        action="store_true",
        help="push to the registry, read and write the registry cache, and also tag :latest",
    )
    _ = output.add_argument(
        "--pull",
        action="store_true",
        help="docker pull a published tag, else build into the local daemon reading the registry cache"
        " (not buildx's --pull, which re-fetches base images)",
    )
    # The old name for --pull, kept so existing callers do not break.
    _ = output.add_argument("--load", dest="pull", action="store_true", help=argparse.SUPPRESS)
    _ = bp.add_argument("--jobs", type=_positive_int, default=2, help="builds to run at a time (default: 2)")
    _ = bp.add_argument("--force", action="store_true", help="build even when the tag is already published")
    _ = bp.add_argument(
        "--no-mirrors",
        action="store_true",
        help="with --push: push to `[registry] url` only, not to `[registry] mirrors`",
    )
    _ = bp.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="id=<id>",
        help="extra buildx secret id; emitted only when its env var is set (repeatable)",
    )
    _ = bp.add_argument("--print", action="store_true", help="print the command line instead of running it")
    bp.set_defaults(func=cmd_build)

    rp = isub.add_parser("registries", help="print the registries `image build --push` pushes to")
    _ = rp.add_argument("--no-mirrors", action="store_true", help="print `[registry] url` only")
    rp.set_defaults(func=cmd_registries)

    cp = isub.add_parser("compose", help="render [[bucket]] placeholders in a compose template")
    _ = cp.add_argument("template")
    _ = cp.add_argument("--head", default="HEAD")
    cp.set_defaults(func=cmd_compose)


def _normalise_secret(value: str) -> str:
    """Accept both `--secret id=x` and the bare `--secret x`."""
    return value[len("id=") :] if value.startswith("id=") else value


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n
