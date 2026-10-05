"""`tangier gate ...` — record a local gate pass, and find it again in CI."""

from __future__ import annotations

import argparse
import os
import shlex
import sys

from tangier import gate
from tangier.config import Config, GateSpec
from tangier.github import emit_outputs
from tangier.runner import Runner, Subprocess


def _runner(args: argparse.Namespace) -> Runner:
    return getattr(args, "runner", None) or Subprocess(echo=True)


def cmd_key(config: Config, args: argparse.Namespace) -> int:
    print(gate.key(config, args.name, args.head))
    return 0


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    """Run each selected gate at HEAD, unless the diff from its comparator does not need it.

    The tree must be clean before and after each run, and HEAD must not move:
    the key describes HEAD, so the commands must test exactly HEAD.
    `--read-only` writes no record, so it drops the rule, and `--dry-run` runs
    nothing. A failing gate does not stop the rest. The exit code is the first
    non-zero one.
    """
    names = _selected(config, args)
    if not args.read_only and not args.dry_run and not gate.is_clean():
        raise gate.GateError(
            f"gate {', '.join(f'`{name}`' for name in names)}: the tree is dirty, so a pass cannot be recorded. "
            "Commit the changes, or use `--read-only` to run without a record"
        )
    # One read of origin for all gates, and none when every record is local.
    origin = gate.OriginRecords()
    first = 0
    for name in names:
        try:
            code = _run_one(config, args, name, origin)
        except gate.GateError as e:
            print(f"error: {e}", file=sys.stderr)
            code = 2
        first = first or code
    return first


def _selected(config: Config, args: argparse.Namespace) -> list[str]:
    if args.all and args.name:
        raise gate.GateError("name gates or pass `--all`, not both")
    if args.all:
        return sorted(config.gates)
    if not args.name:
        raise gate.GateError("name a gate to run, or pass `--all`")
    for name in args.name:
        _ = gate.spec_for(config, name)
    return list(args.name)


def _run_one(config: Config, args: argparse.Namespace, name: str, origin: gate.OriginRecords) -> int:
    """Plan one gate, then run it unless it is verified or not needed.

    A dirty tree reads no record, because no key describes it. A dry run on a
    dirty tree plans as if the tree were committed, as a real run would after
    a commit. `--force` reads no record.
    """
    clean = gate.is_clean()
    if not clean and not args.dry_run:
        print(f"gate `{name}`: the tree is dirty, so no record applies", file=sys.stderr)
    records = clean or (args.dry_run and not args.read_only)
    p = gate.plan(config, name, args.base, origin, records=records, force=args.force)
    if args.debug:
        _print_debug(p)
    if args.dry_run:
        _print_plan(p)
        return 0
    if p.status == "not-needed":
        print(f"gate `{name}`: not-needed for this diff ({p.reason})")
        return 0
    if p.status == "verified":
        print(f"gate `{name}`: verified ({p.reason}), nothing to run")
        return 0

    code = _run_commands(_runner(args), gate.spec_for(config, name), p.commands)
    if code != 0:
        return code
    if args.read_only:
        print(f"gate `{name}`: passed, no record written (--read-only)")
        return 0
    changed = "left the tree dirty" if not gate.is_clean() else "moved HEAD" if gate.head() != p.head else ""
    if changed:
        print(f"gate `{name}`: passed, but the commands {changed}, so no record was written", file=sys.stderr)
        return 1
    ref = gate.write_record(p)
    print(f"gate `{name}`: passed, recorded as {ref}")
    return 0


def _print_plan(p: gate.GatePlan) -> None:
    print(f"gate `{p.name}`: {p.status}")
    print(f"  base {p.effective_base[:7] if p.effective_base else '(none)'} ({p.how})")
    print(f"  why: {p.reason}")
    if p.status == "required":
        for argv in p.commands:
            print(f"  run: {shlex.join(argv)}")


def _print_debug(p: gate.GatePlan) -> None:
    """The comparator walk and the diff from the effective base, to stderr."""
    err = sys.stderr
    print(f"gate `{p.name}`: comparator walk, newest first", file=err)
    for step in p.trail:
        print(f"  {step.commit[:7]} {step.key or '(no key)'} {step.where or 'miss'}", file=err)
    print(f"gate `{p.name}`: effective base {p.effective_base or '(none)'} ({p.how})", file=err)
    print(f"gate `{p.name}`: changed in scope: {', '.join(p.changed) or '(none)'}", file=err)
    for token, items in p.lists.items():
        print(f"gate `{p.name}`: {token}: {','.join(items) or '(empty)'}", file=err)


def _run_commands(runner: Runner, spec: GateSpec, commands: list[list[str]]) -> int:
    """Run each command in turn. Returns the first non-zero exit code, or 0."""
    env = {**os.environ, **spec.env}
    for argv in commands:
        result = runner.run(argv, capture=False, env=env)
        if not result.ok:
            return result.returncode
    return 0


def cmd_verified(config: Config, args: argparse.Namespace) -> int:
    """Print `verified`/`unverified` AND set the exit code, as `image exists` does."""
    if gate.verified(args.name, gate.key(config, args.name, args.head)):
        print("verified")
        return 0
    print("unverified")
    return 1


def cmd_push(config: Config, args: argparse.Namespace) -> int:
    del config, args
    count = gate.push()
    print(f"pushed {count} gate record(s) to {gate.REMOTE}" if count else "no gate records to push")
    return 0


def cmd_github_outputs(config: Config, args: argparse.Namespace) -> int:
    """Emit `<gate>-status`, `<gate>-run`, `<gate>-verified` and `<gate>-key` for every gate.

    `-run` is `true` when the status is `required`, for a plain `if:`.
    """
    # One read of origin for all gates, not one per gate.
    origin = gate.OriginRecords()
    pairs: dict[str, str] = {}
    for name in sorted(config.gates):
        p = gate.plan(config, name, args.base, origin, head=args.head)
        pairs[f"{name}-status"] = p.status
        pairs[f"{name}-run"] = "true" if p.status == "required" else "false"
        pairs[f"{name}-verified"] = "true" if p.status == "verified" else "false"
        pairs[f"{name}-key"] = p.key
    emit_outputs(pairs)
    return 0


def cmd_prune(config: Config, args: argparse.Namespace) -> int:
    del config
    pruned = gate.prune(args.older_than)
    for where, refs in ((gate.REMOTE, pruned.origin), ("local", pruned.local)):
        for ref in refs:
            print(f"deleted {ref} ({where})")
    print(
        f"pruned {len(pruned.origin)} gate record(s) on {gate.REMOTE} and {len(pruned.local)} local, "
        f"older than {args.older_than} days"
    )
    return 0


def _positive_days(value: str) -> int:
    """A day count of at least 1. Zero or less would put the cutoff in the future and delete every record."""
    days = int(value)
    if days < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return days


def _add_diff_args(p: argparse.ArgumentParser, *, head: bool = True) -> None:
    _ = p.add_argument("--base", default="origin/main")
    if head:
        _ = p.add_argument("--head", default="HEAD")


def add_parsers(sub: argparse._SubParsersAction) -> None:
    gp = sub.add_parser("gate", help="record a local gate pass, and reuse it in CI")
    gsub = gp.add_subparsers(dest="cmd", required=True)

    kp = gsub.add_parser("key", help="print a gate's content key")
    _ = kp.add_argument("name")
    # No `--base`: the key reads HEAD's content only.
    _ = kp.add_argument("--head", default="HEAD")
    kp.set_defaults(func=cmd_key)

    rp = gsub.add_parser(
        "run", help="run gates at HEAD, unless the diff from the last verified commit does not need them"
    )
    _ = rp.add_argument("name", nargs="*")
    _ = rp.add_argument("--all", action="store_true", help="run every configured gate, in name order")
    # No `--head`: the commands run against the checked-out tree, so the only
    # head a record can describe is HEAD.
    _add_diff_args(rp, head=False)
    _ = rp.add_argument(
        "--read-only",
        action="store_true",
        help="write no record, and so accept a dirty tree; item lists still come from commits, not the dirty tree",
    )
    _ = rp.add_argument(
        "--force",
        action="store_true",
        help="run the commands from the merge base, even when the gate is not needed or a record exists",
    )
    _ = rp.add_argument(
        "--dry-run", action="store_true", help="print each gate's status, base and commands; run and write nothing"
    )
    _ = rp.add_argument(
        "--debug", action="store_true", help="print the comparator walk and the diff it chose to stderr"
    )
    rp.set_defaults(func=cmd_run)

    vp = gsub.add_parser("verified", help="has this gate passed? prints verified/unverified, exits 0/1")
    _ = vp.add_argument("name")
    _ = vp.add_argument("--head", default="HEAD")
    vp.set_defaults(func=cmd_verified)

    pp = gsub.add_parser("push", help="push local gate records to origin")
    pp.set_defaults(func=cmd_push)

    op = gsub.add_parser("github-outputs", help="emit <gate>-status, -run, -verified and -key as $GITHUB_OUTPUT lines")
    _add_diff_args(op)
    op.set_defaults(func=cmd_github_outputs)

    xp = gsub.add_parser("prune", help="delete old gate records, on origin and in this clone")
    _ = xp.add_argument(
        "--older-than", type=_positive_days, required=True, metavar="DAYS", help="age limit, by the record's time"
    )
    xp.set_defaults(func=cmd_prune)
