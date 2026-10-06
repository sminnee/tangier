"""`tangier gate ...` — record a local gate pass, and find it again in CI."""

from __future__ import annotations

import argparse
import math
import os
import shlex
import sys
import time

from tangier import gate, ranon
from tangier.commands.args import add_diff_args, add_full
from tangier.config import Config, GateSpec, gate_groups, gate_output_name
from tangier.github import emit_outputs
from tangier.runner import Runner, Subprocess

# The run clock. Tests patch this.
clock = time.monotonic


def _runner(args: argparse.Namespace) -> Runner:
    return getattr(args, "runner", None) or Subprocess(echo=True)


def cmd_list(config: Config, args: argparse.Namespace) -> int:
    """Print each gate in config order, with a group's members indented under it."""
    groups = gate_groups(config)
    for name, spec in config.gates.items():
        if not spec.group:
            print(name)
        elif groups[spec.group][0] == name:
            print(f"{spec.group} (group)")
            for member in groups[spec.group]:
                print(f"  {member}")
    return 0


def cmd_key(config: Config, args: argparse.Namespace) -> int:
    """Print the key. A group, or no name, prints `<name> <key>` for each gate."""
    names = list(config.gates) if args.name is None else gate.select(config, args.name)
    tree = gate.snapshot(args.head).tree
    if args.name in config.gates:
        print(gate.key(config, args.name, tree))
        return 0
    # Every key before any output, so a gate that fails closed leaves no partial list.
    keys = [(name, gate.key(config, name, tree)) for name in names]
    for name, key in keys:
        print(f"{name} {key}")
    return 0


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    """Run each selected gate on the working tree, unless the diff from its comparator does not need it.

    The key describes the working tree, uncommitted work included. `--dry-run`
    runs nothing. A failing gate does not stop the rest. The exit code is the
    first non-zero one.
    """
    names = _selected(config, args)
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
        return list(config.gates)
    if not args.name:
        raise gate.GateError("name a gate to run, or pass `--all`")
    return gate.select_all(config, args.name)


def _run_one(config: Config, args: argparse.Namespace, name: str, origin: gate.OriginRecords) -> int:
    """Plan one gate, then run it unless it is verified or not needed.

    Each gate takes its own snapshot, because an earlier gate's commands can
    change the tree. `--full` reads no record and no diff.
    """
    snap = gate.snapshot()
    if snap.dirty:
        touched = gate.uncommitted_in_scope(config, name, snap)
        what = (
            f"uncommitted changes in scope: {', '.join(touched)}"
            if touched
            else "no uncommitted change touches the scope"
        )
        print(f"gate `{name}`: keying the working tree; {what}", file=sys.stderr)
    p = gate.plan(config, name, args.base, snap, origin, full=args.full, accept=args.accept)
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

    start = clock()
    code = _run_commands(_runner(args), gate.spec_for(config, name), p.commands)
    duration = clock() - start
    if code != 0:
        print(f"gate `{name}`: failed in {_took(duration)} (exit {code})")
        return code
    if args.read_only:
        print(f"gate `{name}`: passed in {_took(duration)}, no record written (--read-only)")
        return 0
    # The key is content only, so a moved HEAD over the same tree is fine. A
    # changed tree is not: the commands did not test what the key describes.
    after = gate.snapshot().tree
    if after != snap.tree:
        print(
            f"gate `{name}`: passed in {_took(duration)}, but the working tree changed during the run "
            f"(tree {snap.tree[:7]}, now {after[:7]}), so no record was written",
            file=sys.stderr,
        )
        return 1
    ran_on = ranon.detect()
    ref = gate.write_record(p, ran_on, duration)
    print(f"gate `{name}`: passed in {_took(duration)}, recorded as {ref} ({ran_on['kind']})")
    return 0


def _took(seconds: float) -> str:
    """A run's length: `12.3s` under a minute, `4m05s` from a minute up."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m{rest:02d}s"


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
        print(f"  {step.label} {step.key or '(no key)'} {step.where or 'miss'}", file=err)
        for run, ok in step.runs:
            print(f"    {_ran_on(run)} ({'accepted' if ok else 'ignored'})", file=err)
    print(f"gate `{p.name}`: effective base {p.effective_base or '(none)'} ({p.how})", file=err)
    print(f"gate `{p.name}`: changed in scope: {', '.join(p.changed) or '(none)'}", file=err)
    for token, items in p.lists.items():
        print(f"gate `{p.name}`: {token}: {','.join(items) or '(empty)'}", file=err)


def _ran_on(run: dict[str, object]) -> str:
    """Where, when and for how long a run ran: `ci github-actions push refs/heads/main job=e2e <time> 12.3s`."""
    runner = run.get("runner")
    runner = runner if isinstance(runner, dict) else {}
    if runner.get("kind") == "ci":
        parts = [runner.get(name) for name in ("kind", "provider", "event", "ref")]
        if runner.get("job"):
            parts.append(f"job={runner['job']}")
    else:
        where = "@".join(str(part) for part in (run.get("user"), runner.get("host")) if part)
        parts = [runner.get("kind", "local"), where]
    duration = run.get("duration")
    # Records come from origin, so a hand-edited `duration` must not crash `--debug`.
    ok = isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration)
    took = _took(float(duration)) if ok else None
    return " ".join(str(part) for part in [*parts, run.get("time"), took] if part)


def _run_commands(runner: Runner, spec: GateSpec, commands: list[list[str]]) -> int:
    """Run each command in turn. Returns the first non-zero exit code, or 0."""
    env = {**os.environ, **spec.env}
    for argv in commands:
        result = runner.run(argv, capture=False, env=env)
        if not result.ok:
            return result.returncode
    return 0


def cmd_verified(config: Config, args: argparse.Namespace) -> int:
    """Print `verified`/`unverified` AND set the exit code, as `image exists` does.

    A group is verified only when every member is.
    """
    names = gate.select(config, args.name)
    tree = gate.snapshot(args.head).tree
    # One exact-ref lookup for one gate. A group lists origin's gate refs once for all its members.
    origin = gate.OriginRecords() if len(names) > 1 else None
    if all(gate.verified(name, gate.key(config, name, tree), origin, args.accept) for name in names):
        print("verified")
        return 0
    print("unverified")
    return 1


def cmd_push(config: Config, args: argparse.Namespace) -> int:
    del config, args
    synced = gate.sync()
    if synced.pushed or synced.pulled or synced.merged:
        print(
            f"synced gate records with {gate.REMOTE}: "
            f"pushed {synced.pushed}, pulled {synced.pulled}, merged {synced.merged}"
        )
    else:
        print(f"gate records already in sync with {gate.REMOTE}")
    return 0


def cmd_github_outputs(config: Config, args: argparse.Namespace) -> int:
    """Emit `<gate>-status`, `-run`, `-verified` and `-key` for every gate, then the first three for every group.

    `-run` is `true` when the status is `required`, for a plain `if:`. A `.`
    in a name becomes `-`.
    """
    # One read of origin for all gates, not one per gate. `--full` reads none.
    origin = gate.OriginRecords()
    snap = gate.snapshot(args.head)
    statuses = {
        name: gate.plan(config, name, args.base, snap, origin, full=args.full, accept=args.accept)
        for name in sorted(config.gates)
    }
    pairs: dict[str, str] = {}
    for name, p in statuses.items():
        _add_status(pairs, gate_output_name(name), p.status)
        pairs[f"{gate_output_name(name)}-key"] = p.key
    for group, members in sorted(gate_groups(config).items()):
        _add_status(pairs, gate_output_name(group), _group_status([statuses[m].status for m in members]))
    emit_outputs(pairs)
    return 0


def _add_status(pairs: dict[str, str], prefix: str, status: str) -> None:
    pairs[f"{prefix}-status"] = status
    pairs[f"{prefix}-run"] = "true" if status == "required" else "false"
    pairs[f"{prefix}-verified"] = "true" if status == "verified" else "false"


def _group_status(statuses: list[str]) -> str:
    """`required` if any member is, else `verified` if every member is, else `not-needed`."""
    if "required" in statuses:
        return "required"
    if all(status == "verified" for status in statuses):
        return "verified"
    return "not-needed"


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


def _accept(value: str) -> gate.Accept:
    try:
        return gate.Accept.parse(value)
    except gate.GateError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _add_accept(p: argparse.ArgumentParser) -> None:
    _ = p.add_argument(
        "--accept",
        action="append",
        type=_accept,
        default=[],
        metavar="RAN_ON",
        help="count only runs on this runner: `ci`, `local`, or `field=value,...` over "
        f"{', '.join(gate.ACCEPT_FIELDS)}; repeat to accept any of several",
    )


def _add_head_arg(p: argparse.ArgumentParser) -> None:
    _ = p.add_argument("--head", default=None, help="key this commit, not the working tree")


def add_parsers(sub: argparse._SubParsersAction) -> None:
    gp = sub.add_parser("gate", help="record a local gate pass, and reuse it in CI")
    gsub = gp.add_subparsers(dest="cmd", required=True)

    lp = gsub.add_parser("list", help="list every gate and group")
    lp.set_defaults(func=cmd_list)

    kp = gsub.add_parser("key", help="print a gate's content key; a group, or no name, prints `<name> <key>` per gate")
    _ = kp.add_argument("name", nargs="?", help="a gate or a group; omit for every gate")
    # No `--base`: the key reads content only.
    _add_head_arg(kp)
    kp.set_defaults(func=cmd_key)

    rp = gsub.add_parser(
        "run", help="run gates on the working tree, unless the diff from the last verified commit does not need them"
    )
    _ = rp.add_argument("name", nargs="*", help="a gate, or a group for every gate in it")
    _ = rp.add_argument("--all", action="store_true", help="run every configured gate, in config order")
    # No `--head`: the commands run against the checked-out tree, so the only
    # content a record can describe is the working tree.
    add_diff_args(rp, head=False)
    _ = rp.add_argument(
        "--read-only",
        action="store_true",
        help="reuse a record, but write none",
    )
    add_full(rp, "run with complete lists, as if every tag changed; reads no record and no diff")
    _ = rp.add_argument(
        "--dry-run", action="store_true", help="print each gate's status, base and commands; run and write nothing"
    )
    _ = rp.add_argument(
        "--debug", action="store_true", help="print the comparator walk and the diff it chose to stderr"
    )
    _add_accept(rp)
    rp.set_defaults(func=cmd_run)

    vp = gsub.add_parser("verified", help="has this gate passed? prints verified/unverified, exits 0/1")
    _ = vp.add_argument("name", help="a gate, or a group, which is verified when every member is")
    _add_head_arg(vp)
    _add_accept(vp)
    vp.set_defaults(func=cmd_verified)

    pp = gsub.add_parser("push", help="sync gate records with origin: pull, merge runs, push")
    pp.set_defaults(func=cmd_push)

    op = gsub.add_parser("github-outputs", help="emit <gate>-status, -run, -verified and -key as $GITHUB_OUTPUT lines")
    add_diff_args(op)
    _add_accept(op)
    add_full(op, "mark every gate and group required, as if every tag changed")
    op.set_defaults(func=cmd_github_outputs)

    xp = gsub.add_parser("prune", help="delete old gate records, on origin and in this clone")
    _ = xp.add_argument(
        "--older-than", type=_positive_days, required=True, metavar="DAYS", help="age limit, by the record's time"
    )
    xp.set_defaults(func=cmd_prune)
