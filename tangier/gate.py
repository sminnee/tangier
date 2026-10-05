"""Gate records: a local gate pass that CI can find and reuse.

The key is a hash of content, and the store is git refs. See
`docs/specs/gate.md`.

Git goes through `tangier.git`. The commands themselves go through a `Runner`,
in `commands/gate_cmds.py`.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Container
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from tangier import __version__, git
from tangier.changemap import AnswerSet, compute_answer_set, scope_lines, scope_touched
from tangier.config import ITEMS_PLACEHOLDER_SUFFIX, Config, GateSpec, entry_tags, is_placeholder, scope_tags

REF_PREFIX = "refs/tangier/gates"
# Where `prune` mirrors origin's gate refs, apart from this clone's own records.
ORIGIN_MIRROR_PREFIX = "refs/tangier/origin-gates"
REMOTE = "origin"

# Bump when the key's inputs or their encoding change: every record then misses.
KEY_VERSION = 2


class GateError(RuntimeError):
    """A gate cannot be keyed, run or recorded."""


@dataclass
class Step:
    """One commit the comparator walk looked at."""

    commit: str
    # None when no key can be made at this commit, such as when a scope entry matches nothing there.
    key: str | None
    # `verified()`'s answer for the key.
    where: str | None


@dataclass
class Comparator:
    """The commit a gate diffs from. See `docs/specs/gate.md#comparator`."""

    # None when `base` has no merge base with the head. Only a gate with no placeholder gets this far.
    commit: str | None
    # `local` or `origin` when a record chose the commit. None for the merge-base fallback.
    where: str | None
    # How the commit was chosen, for people: `record at abc1234, local` or `merge base with origin/main`.
    how: str
    trail: list[Step]


@dataclass
class GatePlan:
    """What `gate run` does for one gate at one head, and why."""

    name: str
    # `verified`, `not-needed` or `required`. See `docs/specs/gate.md#status-values`.
    status: str
    # The key at `head`, which a pass records.
    key: str
    head: str
    # The comparator: the commit the diff starts from.
    effective_base: str | None
    where: str | None
    how: str
    # Resolved against `effective_base`.
    commands: list[list[str]]
    reason: str
    trail: list[Step]
    # The changed files that touch the scope.
    changed: list[str]
    # Each placeholder token's list.
    lists: dict[str, list[str]]


def now() -> datetime:
    """The record clock. Tests patch this."""
    return datetime.now(UTC)


def spec_for(cfg: Config, name: str) -> GateSpec:
    spec = cfg.gates.get(name)
    if spec is None:
        known = ", ".join(sorted(cfg.gates)) or "(none)"
        raise GateError(f"no `[gate.{name}]` in config (configured: {known})")
    return spec


def placeholder_lists(spec: GateSpec, answers: AnswerSet) -> dict[str, list[str]]:
    """Each placeholder token in the commands, mapped to its list.

    The parser has already checked each placeholder against the config, so a
    lookup here cannot miss.
    """
    lists: dict[str, list[str]] = {}
    for token in (token for argv in spec.commands for token in argv if is_placeholder(token)):
        name = token[1:-1]
        if name.endswith(ITEMS_PLACEHOLDER_SUFFIX):
            lists[token] = answers.items[name[: -len(ITEMS_PLACEHOLDER_SUFFIX)]]
        else:
            lists[token] = answers.file_sets[name]
    return lists


def resolve_commands(spec: GateSpec, lists: dict[str, list[str]]) -> list[list[str]]:
    """Replace each placeholder token with its comma-joined list.

    An empty list gives an empty argument, so the argv shape does not depend on
    the diff.
    """
    return [[",".join(lists[token]) if is_placeholder(token) else token for token in argv] for argv in spec.commands]


def has_placeholder(spec: GateSpec) -> bool:
    return any(is_placeholder(token) for argv in spec.commands for token in argv)


def _commit(name: str, head: str) -> str:
    try:
        return git.rev_parse(head)
    except git.GitError as e:
        raise GateError(f"gate `{name}`: `{head}` does not name a commit ({e})") from e


def key(cfg: Config, name: str, head: str = "HEAD") -> str:
    """The gate's key at `head`: a hash of its raw commands, its `env`, and its scope's content.

    The placeholders stay unresolved, so the key reads no diff and no base: a
    record at a commit means the gate passed for the scope as it stands there.

    Fails closed, because the lenient git helpers turn each of these into a
    stable key that many trees share:
      - a bad `head` walks as an empty tree;
      - a scope entry that matches no file hashes no content.
    """
    spec = spec_for(cfg, name)
    _ = _commit(name, head)
    for entry in spec.scope:
        if not scope_lines(cfg, entry_tags(cfg, entry), head):
            raise GateError(f"gate `{name}`: the scope entry `{entry}` matches no tracked file at `{head}`")
    lines = scope_lines(cfg, scope_tags(cfg, spec), head)
    header = json.dumps(
        {"v": KEY_VERSION, "gate": name, "commands": spec.commands, "env": spec.env},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha1("\n".join([header, *lines]).encode()).hexdigest()


def comparator(
    cfg: Config, name: str, base: str, head: str = "HEAD", origin: Container[str] | None = None, *, records: bool = True
) -> Comparator:
    """The newest commit, from `head` back to its merge base with `base`, whose content has a record.

    `head` comes first. A gate with no placeholder stops there: an older record
    cannot narrow its commands, so it then diffs from the merge base, and needs
    none when `base` cannot be read. A gate with placeholders walks `head`'s
    first-parent line down to the merge base, and falls back to the merge base
    with no hit. `records=False` reads no record and gives the merge base.
    """
    spec = spec_for(cfg, name)
    head_commit = _commit(name, head)
    found = OriginRecords(f"{REF_PREFIX}/{name}/*") if origin is None else origin
    trail: list[Step] = []

    def check(commit: str) -> Step:
        try:
            k = key(cfg, name, commit)
        except GateError:
            # Only `head` must have a key. An older commit with none holds no record.
            if commit == head_commit:
                raise
            step = Step(commit, None, None)
        else:
            step = Step(commit, k, verified(name, k, found))
        trail.append(step)
        return step

    if records and check(head_commit).where:
        return Comparator(head_commit, trail[0].where, f"record at {head_commit[:7]}, {trail[0].where}", trail)
    try:
        mb = git.merge_base(base, head_commit)
    except git.GitError as e:
        if has_placeholder(spec):
            raise GateError(
                f"gate `{name}`: cannot diff `{base}` against `{head}`, so the commands cannot be resolved. "
                f"Fetch `{base}` with enough history to reach the merge base, or pass `--base` ({e})"
            ) from e
        return Comparator(None, None, f"no merge base with {base}: {e}", trail)
    if records and has_placeholder(spec):
        for commit in [*git.rev_list_first_parent(head_commit, mb), mb]:
            if commit != head_commit and (step := check(commit)).where:
                return Comparator(commit, step.where, f"record at {commit[:7]}, {step.where}", trail)
    return Comparator(mb, None, f"merge base with {base}", trail)


def plan(
    cfg: Config,
    name: str,
    base: str,
    origin: Container[str] | None = None,
    *,
    head: str = "HEAD",
    records: bool = True,
    force: bool = False,
) -> GatePlan:
    """What a run of the gate at `head` does. See `docs/specs/gate.md#status-values`.

    `verified` when the comparator is `head`. Otherwise the diff from the
    comparator decides: `not-needed` when no key input changed or every
    placeholder list is empty, and `required` otherwise. `force` reads no
    record and is always `required`, so it diffs from the merge base.
    """
    spec = spec_for(cfg, name)
    head_key = key(cfg, name, head)
    comp = comparator(cfg, name, base, head, origin, records=records and not force)
    head_commit = git.rev_parse(head)
    changed: list[str] = []
    lists: dict[str, list[str]] = {}

    def made(status: str, reason: str, commands: list[list[str]]) -> GatePlan:
        return GatePlan(
            name=name,
            status=status,
            key=head_key,
            head=head_commit,
            effective_base=comp.commit,
            where=comp.where,
            how=comp.how,
            commands=commands,
            reason=reason,
            trail=comp.trail,
            changed=changed,
            lists=lists,
        )

    if comp.commit == head_commit and comp.where:
        return made("verified", f"{comp.where} record {head_key}", [])
    if comp.commit is None:
        if not force:
            print(f"warning: gate `{name}`: {comp.how}, so it counts as needed", file=sys.stderr)
        return made("required", comp.how, [list(argv) for argv in spec.commands])

    changed = [f for f in git.changed_files(comp.commit, head) if scope_touched(cfg, scope_tags(cfg, spec), [f])]
    lists = (
        placeholder_lists(spec, compute_answer_set(cfg, comp.commit, head, compute_shas=False))
        if has_placeholder(spec)
        else {}
    )
    commands = resolve_commands(spec, lists)
    if force:
        return made("required", "--force", commands)
    if not changed:
        return made("not-needed", f"no key input changed since {comp.how}", commands)
    if lists and not any(lists.values()):
        return made("not-needed", "every placeholder list is empty", commands)
    return made("required", f"{len(changed)} key input(s) changed since {comp.how}", commands)


def ref_for(name: str, key: str) -> str:
    return f"{REF_PREFIX}/{name}/{key}"


def write_record(plan: GatePlan) -> str:
    """Record a pass of the planned gate as a local ref. Returns the ref."""
    record = {
        "gate": plan.name,
        "key": plan.key,
        "head": plan.head,
        "base": plan.effective_base,
        "user": git.config_get("user.email") or "unknown",
        "time": now().isoformat(timespec="seconds"),
        "tangier": __version__,
        "commands": plan.commands,
    }
    ref = ref_for(plan.name, plan.key)
    git.update_ref(ref, git.hash_object(json.dumps(record, indent=2, sort_keys=True) + "\n"))
    return ref


def read_record(sha: str) -> dict[str, object]:
    """The record a gate ref points at. Raises `GateError` when it is not one."""
    try:
        record = json.loads(git.cat_blob(sha))
    except (git.GitError, ValueError) as e:
        raise GateError(f"not a gate record: {e}") from e
    if not isinstance(record, dict):
        raise GateError("not a gate record: the blob is not a JSON object")
    return record


def head() -> str:
    """The commit HEAD names now."""
    return git.rev_parse("HEAD")


def is_clean() -> bool:
    """Whether the tree has no tracked change and no untracked file."""
    return not git.status_porcelain()


def origin_records(pattern: str = f"{REF_PREFIX}/*") -> set[str]:
    """The gate refs on origin that match `pattern`. Empty, with a warning, when origin cannot be read.

    An origin that cannot be reached counts as holding no record: the caller
    then runs the gate, which is the safe direction.
    """
    try:
        return {ref for _, ref in git.ls_remote(REMOTE, pattern)}
    except git.GitError as e:
        print(f"warning: cannot read gate records from {REMOTE}, so they count as absent: {e}", file=sys.stderr)
        return set()


class OriginRecords:
    """The gate refs on origin that match `pattern`, read once, at the first lookup.

    A caller that finds every record locally then reads no network.
    """

    def __init__(self, pattern: str = f"{REF_PREFIX}/*") -> None:
        self.pattern = pattern
        self._refs: set[str] | None = None

    def __contains__(self, ref: object) -> bool:
        if self._refs is None:
            self._refs = origin_records(self.pattern)
        return ref in self._refs


def verified(name: str, key: str, origin: Container[str] | None = None) -> str | None:
    """Where a record for this key is: `local`, `origin`, or None.

    Local first, since it needs no network. `origin` is shared by a caller
    that checks several keys, so origin is read once.
    """
    ref = ref_for(name, key)
    if git.ref_exists(ref):
        return "local"
    # Membership, not "any result": `ls-remote` matches a pattern against
    # trailing path components, and only the exact ref is a record.
    if ref in (OriginRecords(ref) if origin is None else origin):
        return "origin"
    return None


def push() -> int:
    """Push every local gate ref to origin. Returns how many there were.

    Forced: two people can record the same key with different blobs. The last
    writer wins, and both blobs mean the same pass.
    """
    count = len(git.for_each_ref(REF_PREFIX))
    if count:
        git.push(REMOTE, [f"+{REF_PREFIX}/*:{REF_PREFIX}/*"])
    return count


@dataclass
class Pruned:
    origin: list[str]
    local: list[str]


def prune(older_than_days: int) -> Pruned:
    """Delete gate refs whose record is older than the limit, on origin and in this clone.

    Age is the record's own `time`, not a ref or commit date: a record is a
    blob and carries no other date. A ref that is not a readable record is left
    alone, with a warning.

    The local records go too because `push` sends every local ref: an old one
    left here would put its pruned ref back on origin.
    """
    git.fetch(REMOTE, f"+{REF_PREFIX}/*:{ORIGIN_MIRROR_PREFIX}/*")
    cutoff = now() - timedelta(days=older_than_days)
    on_origin = _expired(ORIGIN_MIRROR_PREFIX, cutoff)
    if on_origin:
        git.push(REMOTE, [f":{ref}" for ref in on_origin])
    local = _expired(REF_PREFIX, cutoff)
    for ref in local:
        git.delete_ref(ref)
    return Pruned(origin=on_origin, local=local)


def _expired(prefix: str, cutoff: datetime) -> list[str]:
    """The gate refs whose record is older than `cutoff`, read from the refs under `prefix`.

    Each is returned under its `REF_PREFIX` name, which is the name on origin
    for a mirrored ref.
    """
    old: list[str] = []
    for sha, found in git.for_each_ref(prefix):
        ref = REF_PREFIX + found[len(prefix) :]
        try:
            recorded = datetime.fromisoformat(str(read_record(sha)["time"]))
            if recorded < cutoff:
                old.append(ref)
        except (GateError, KeyError, ValueError, TypeError) as e:
            print(f"warning: skipped {ref}: {e}", file=sys.stderr)
    return old
