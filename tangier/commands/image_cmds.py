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
    config_platform_argv,
    copy_argv,
    destination_refs,
    exists_argv,
    has_platform,
    image_ref,
    index_platforms,
    leg_tag,
    manifest_argv,
    merge_argv,
    native_platform_argv,
    parse_platforms,
    plan_build,
    plan_merge,
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


def published(runner: Runner, ref: str, tag: str, platform: str = "linux/amd64") -> bool:
    """Whether `<ref>:<tag>` is already in its registry.

    A missing `regctl` raises rather than reporting "missing". The old script
    printed `missing` when regctl was absent, which caused a rebuild-and-push of
    an already-published tag — a fail-open that costs a full build and moves
    `:latest` for no reason.
    """
    _need_regctl(runner)
    return runner.run(exists_argv(ref, tag, platform)).ok


def _need_regctl(runner: Runner) -> None:
    if runner.which("regctl") is None:
        raise ImageError("regctl not found on PATH — cannot check whether the tag is published")


def platforms_at(runner: Runner, ref: str, tag: str) -> list[str] | None:
    """The platforms `<ref>:<tag>` holds, or None when the tag is not published.

    Reads the index's platform list, or a single manifest's config: see
    `manifest_argv` for why `published` cannot answer this.
    """
    _need_regctl(runner)
    result = runner.run(manifest_argv(ref, tag))
    if not result.ok:
        return None
    found = index_platforms(result.stdout)
    if found is not None:
        return found
    config = runner.run(config_platform_argv(ref, tag))
    if not config.ok:
        raise ImageError(f"cannot read the platform of {ref}:{tag}: {config.stderr.strip()}")
    return [config.stdout.strip()]


def _native_platform(runner: Runner) -> str:
    """The Docker daemon's platform: the one a `--leg` build produces."""
    if runner.which("docker") is None:
        raise ImageError("docker not found on PATH — cannot build")
    result = runner.run(native_platform_argv())
    platform = result.stdout.strip()
    if not result.ok or "/" not in platform:
        raise ImageError(f"cannot read the Docker daemon's platform: {(result.stderr or result.stdout).strip()}")
    return platform


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

    `--leg` pushes one platform's leg of a multi-platform image, for
    `image merge` to join: the daemon's own platform, to the primary only, as
    `<tag>-<os>-<arch>`. It is reused when the final tag already holds that
    platform or the leg tag exists.
    """
    names: list[str] = list(dict.fromkeys(args.buckets))
    if args.tag and len(names) > 1:
        raise ImageError("--tag names one image, so it takes one bucket")
    push = not args.pull
    if args.leg and not push:
        raise ImageError("--leg builds one platform of a pushed image, so it needs --push")
    runner = _runner(args)
    extra_secrets = [_normalise_secret(s) for s in (args.secret or [])]
    # Mirrors are push destinations only: a pull reads `url`. A leg is not the
    # finished image, so it goes to the primary alone.
    with_mirrors = push and not args.no_mirrors and not args.leg
    leg: str | None = None

    def build_cmd(bucket: str, tag: str) -> list[str]:
        spec = config.images[bucket]
        refs = destination_refs(config, bucket, mirrors=with_mirrors)
        secrets = resolve_secrets(spec.secrets, extra_secrets)
        return build_argv(
            spec, refs[0], tag, push=push, secrets=secrets, mirrors=refs[1:], leg=leg, extra=args.buildx_arg
        )

    # `--print` is a dry run, so it must not need a registry: probing first
    # would make it fail on exactly the machine most likely to want it. It
    # therefore emits no outputs either — there is no build to describe.
    if args.print:
        if args.leg:
            # Reading the real platform would need a Docker daemon.
            leg = "linux/amd64"
            print("--leg --print shows the linux/amd64 leg; a real run builds the daemon's platform", file=sys.stderr)
        steps = plan_build(config, names, args.head, force=True, is_published=lambda b, t: False, tag=args.tag)
        for step in steps:
            print(" ".join(build_cmd(step.bucket, step.tag)))
        return 0

    dirty = _dirty_buckets(config, names, args.head)
    if push:
        _refuse_dirty(dirty)

    if args.leg:
        leg = _native_platform(runner)

    # Which destinations already hold each bucket's tag, from the `--push` probe.
    have: dict[str, list[str]] = {}

    def is_published(bucket: str, tag: str) -> bool:
        if not push:
            return published(runner, image_ref(config, bucket), tag)
        if leg:
            ref = image_ref(config, bucket)
            final = platforms_at(runner, ref, tag)
            if final is not None and has_platform(final, leg):
                print(f"{ref}:{tag} already holds {leg}; skipping build")
            elif published(runner, ref, leg_tag(tag, leg), leg):
                print(f"{ref}:{leg_tag(tag, leg)} already published; skipping build")
            else:
                return False
            return True
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
        if leg:
            outcome[step.bucket] = ("skip", 0)
        elif push:
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


def _refuse_dirty(dirty: dict[str, list[str]]) -> None:
    """Stop a push of a bucket with uncommitted changes."""
    if dirty:
        detail = "; ".join(f"{bucket}: {', '.join(files)}" for bucket, files in dirty.items())
        raise ImageError(
            "uncommitted changes would be pushed under a tag that hashes the committed tree. "
            f"Commit or remove them first ({detail})"
        )


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


def cmd_merge(config: Config, args: argparse.Namespace) -> int:
    """Join each bucket's `--leg` pushes into one multi-platform image, and publish it.

    `plan_merge` decides per bucket. See "Multi-platform images" in
    docs/guide/4-packages-and-builds.md.
    """
    names: list[str] = list(dict.fromkeys(args.buckets))
    if args.tag and len(names) > 1:
        raise ImageError("--tag names one image, so it takes one bucket")
    platforms = parse_platforms(args.platforms)
    runner = _runner(args)
    _refuse_dirty(_dirty_buckets(config, names, args.head))

    steps = plan_merge(
        config,
        names,
        platforms,
        args.head,
        mirrors=not args.no_mirrors,
        tag=args.tag,
        platforms_at=lambda ref, tag: platforms_at(runner, ref, tag),
        # A leg tag holds only the platform it names, so plain existence is enough.
        leg_exists=lambda ref, tag, platform: published(runner, ref, leg_tag(tag, platform), platform),
    )
    if any(s.action == "merge" for s in steps) and runner.which("docker") is None:
        raise ImageError("docker not found on PATH — cannot merge")

    rc = 0
    outcome: dict[str, int] = {}
    for step in steps:
        primary = step.refs[0]
        if step.action == "skip":
            print(f"{primary}:{step.tag} already holds {', '.join(platforms)} everywhere; skipping merge")
            outcome[step.bucket] = 0
        elif step.action == "copy":
            outcome[step.bucket] = _copy_tag_and_latest(runner, primary, step.lacking, step.tag)
        else:
            joined = runner.run(merge_argv(step.sources, [f"{primary}:{step.tag}", f"{primary}:latest"]), capture=False)
            ok = joined.ok
            outcome[step.bucket] = (
                _copy_tag_and_latest(runner, primary, step.refs[1:], step.tag) if ok else joined.returncode
            )
        rc = rc or outcome[step.bucket]

    done = {"merge": "merged", "copy": "copied", "skip": "skipped"}
    for step in steps:
        code = outcome[step.bucket]
        print(f"{done[step.action] if code == 0 else step.action + ' failed'} {step.bucket}")

    if len(steps) == 1:
        [step] = steps
        built = step.action == "merge" and outcome[step.bucket] == 0
        emit_outputs({"tag": step.tag, "built": "true" if built else "false"})
    return rc


def _copy_tag_and_latest(runner: Runner, primary: str, dests: list[str], tag: str) -> int:
    """Copy the primary's merged `:<tag>` and `:latest` to each of `dests`, stopping at a failure.

    Unlike `_fill_from`, `:latest` moves too: a merge publishes it, so a re-run
    after a failed copy must finish the job.
    """
    for dest in dests:
        for t in (tag, "latest"):
            copied = runner.run(copy_argv(f"{primary}:{t}", f"{dest}:{t}"), capture=False)
            if not copied.ok:
                return copied.returncode
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
    _ = bp.add_argument(
        "--leg",
        action="store_true",
        help="with --push: push this machine's platform as one leg of a multi-platform image,"
        " as <tag>-<os>-<arch> on `[registry] url` only, for `image merge` to join",
    )
    _ = bp.add_argument(
        "--buildx-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="extra argument passed verbatim to `docker buildx build`; write it as --buildx-arg=--flag (repeatable)",
    )
    _ = bp.add_argument("--print", action="store_true", help="print the command line instead of running it")
    bp.set_defaults(func=cmd_build)

    mp = isub.add_parser(
        "merge",
        help="join each bucket's `image build --leg` pushes into one multi-platform image, and publish it",
    )
    _ = mp.add_argument("buckets", nargs="+", metavar="bucket")
    _ = mp.add_argument(
        "--platforms",
        required=True,
        help="comma-separated platforms the image must hold, such as linux/amd64,linux/arm64",
    )
    _ = mp.add_argument(
        "--tag", default=None, help="tag to merge (default: the computed content hash); one bucket only"
    )
    _ = mp.add_argument("--head", default="HEAD")
    _ = mp.add_argument(
        "--no-mirrors", action="store_true", help="publish to `[registry] url` only, not to `[registry] mirrors`"
    )
    mp.set_defaults(func=cmd_merge)

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
