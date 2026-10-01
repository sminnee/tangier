"""`tangier gate ...` — record a local gate pass, and find it again in CI."""

from __future__ import annotations

import argparse
import os
import sys

from tangier import gate
from tangier.config import Config, GateSpec
from tangier.github import emit_outputs
from tangier.runner import Runner, Subprocess


def _runner(args: argparse.Namespace) -> Runner:
    return getattr(args, "runner", None) or Subprocess(echo=True)


def cmd_key(config: Config, args: argparse.Namespace) -> int:
    print(gate.key(config, args.name, args.base, args.head))
    return 0


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    """Run a gate at HEAD, unless a record shows it has already passed.

    The tree must be clean before and after the run, and HEAD must not move:
    the key describes HEAD, so the commands must test exactly HEAD.
    `--no-record` drops the rule and the record together.
    """
    name = args.name
    spec = gate.spec_for(config, name)
    runner = _runner(args)
    if args.no_record:
        return _run_commands(runner, spec, gate.commands_for(config, name, args.base, "HEAD"))

    if not gate.is_clean():
        raise gate.GateError(
            f"gate `{name}`: the tree is dirty, so a pass cannot be recorded. "
            "Commit the changes, or use `--no-record` to run without a record"
        )
    resolved = gate.resolve(config, name, args.base, "HEAD")
    where = gate.verified(name, resolved.key)
    if where:
        print(f"gate `{name}`: verified ({where} record {resolved.key}), nothing to run")
        return 0

    code = _run_commands(runner, spec, resolved.commands)
    if code != 0:
        return code
    changed = "left the tree dirty" if not gate.is_clean() else "moved HEAD" if gate.head() != resolved.head else ""
    if changed:
        print(f"gate `{name}`: passed, but the commands {changed}, so no record was written", file=sys.stderr)
        return 1
    ref = gate.write_record(name, resolved)
    print(f"gate `{name}`: passed, recorded as {ref}")
    return 0


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
    if gate.verified(args.name, gate.key(config, args.name, args.base, args.head)):
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
    """Emit `<gate>-verified` and `<gate>-key` for every gate."""
    # One read of origin for all gates, not one per gate.
    origin = gate.origin_records()
    pairs: dict[str, str] = {}
    for name in sorted(config.gates):
        key = gate.key(config, name, args.base, args.head)
        pairs[f"{name}-verified"] = "true" if gate.verified(name, key, origin) else "false"
        pairs[f"{name}-key"] = key
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
    _add_diff_args(kp)
    kp.set_defaults(func=cmd_key)

    rp = gsub.add_parser("run", help="run a gate at HEAD, unless a record shows it has passed")
    _ = rp.add_argument("name")
    # No `--head`: the commands run against the checked-out tree, so the only
    # head a record can describe is HEAD.
    _add_diff_args(rp, head=False)
    _ = rp.add_argument(
        "--no-record",
        action="store_true",
        help="always run, on any tree, and write no record; item lists still come from commits, not the dirty tree",
    )
    rp.set_defaults(func=cmd_run)

    vp = gsub.add_parser("verified", help="has this gate passed? prints verified/unverified, exits 0/1")
    _ = vp.add_argument("name")
    _add_diff_args(vp)
    vp.set_defaults(func=cmd_verified)

    pp = gsub.add_parser("push", help="push local gate records to origin")
    pp.set_defaults(func=cmd_push)

    op = gsub.add_parser("github-outputs", help="emit <gate>-verified and <gate>-key as $GITHUB_OUTPUT lines")
    _add_diff_args(op)
    op.set_defaults(func=cmd_github_outputs)

    xp = gsub.add_parser("prune", help="delete old gate records, on origin and in this clone")
    _ = xp.add_argument(
        "--older-than", type=_positive_days, required=True, metavar="DAYS", help="age limit, by the record's time"
    )
    xp.set_defaults(func=cmd_prune)
