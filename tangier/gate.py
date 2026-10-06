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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from tangier import __version__, git
from tangier.changemap import AnswerSet, answer_set_for_files, full_answer_set, scope_lines, scope_touched
from tangier.config import (
    ITEMS_PLACEHOLDER_SUFFIX,
    Config,
    GateSpec,
    entry_tags,
    gate_groups,
    is_placeholder,
    scope_tags,
)

REF_PREFIX = "refs/tangier/gates"
# Where a fetch mirrors origin's gate refs, apart from this clone's own records.
ORIGIN_MIRROR_PREFIX = "refs/tangier/origin-gates"
REMOTE = "origin"

# Bump when the key's inputs or their encoding change: every record then misses.
KEY_VERSION = 2

# The record blob's shape. A blob with no `runs` is a legacy single run.
RECORD_FORMAT = 2
# The most runs one record keeps. The oldest go first.
MAX_RUNS = 20

# The runner fields an `--accept` value can name, and the kinds a runner can be.
ACCEPT_FIELDS = ("kind", "provider", "event", "ref", "workflow", "job")
RUNNER_KINDS = ("local", "ci")


class GateError(RuntimeError):
    """A gate cannot be keyed, run or recorded."""


@dataclass
class Snapshot:
    """The content a gate is keyed on, and the commit it sits on."""

    commit: str
    tree: str
    # Whether `tree` holds uncommitted work, so differs from `commit`'s tree.
    dirty: bool

    @property
    def label(self) -> str:
        """`abc1234`, or `working tree (def5678)` when the tree is dirty."""
        return f"working tree ({self.tree[:7]})" if self.dirty else self.commit[:7]


@dataclass
class Step:
    """One commit, or the working tree, that the comparator walk looked at."""

    commit: str
    # How `--debug` names it: a short SHA, or `working tree (<tree>)`.
    label: str
    # None when no key can be made at this commit, such as when a scope entry matches nothing there.
    key: str | None
    # `verified()`'s answer for the key.
    where: str | None
    # Every run read at this commit, each with whether `--accept` took it.
    runs: list[tuple[dict[str, object], bool]] = field(default_factory=list)


@dataclass(frozen=True)
class Accept:
    """One `--accept` value: the runs whose runner has every named field. See `docs/specs/gate.md#cli`."""

    fields: tuple[tuple[str, str], ...]

    @classmethod
    def parse(cls, value: str) -> Accept:
        """A bare kind, such as `ci`, or comma-separated `field=value` pairs."""
        pairs: list[tuple[str, str]] = []
        for part in value.split(","):
            name, eq, wanted = part.partition("=")
            if not eq:
                name, wanted = "kind", part
            if name not in ACCEPT_FIELDS:
                raise GateError(f"`{name}` is not a runner field (use {', '.join(ACCEPT_FIELDS)})")
            if not wanted:
                raise GateError(f"`{part}` names no value")
            if name == "kind" and wanted not in RUNNER_KINDS:
                raise GateError(f"`{wanted}` is not a runner kind (use {' or '.join(RUNNER_KINDS)})")
            pairs.append((name, wanted))
        return cls(tuple(pairs))

    def matches(self, runner: Mapping[str, object]) -> bool:
        return all(runner.get(name) == wanted for name, wanted in self.fields)


def accepted(run: Mapping[str, object], accept: Sequence[Accept]) -> bool:
    """Whether a run counts: any `--accept` value matches it, or there is none."""
    runner = run.get("runner")
    return not accept or (isinstance(runner, Mapping) and any(a.matches(runner) for a in accept))


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
    # Whether the snapshot itself has a record. The commit can be HEAD without
    # this, when the tree is dirty and HEAD's record is the comparator.
    verified: bool = False


@dataclass
class GatePlan:
    """What `gate run` does for one gate at one snapshot, and why."""

    name: str
    # `verified`, `not-needed` or `required`. See `docs/specs/gate.md#status-values`.
    status: str
    # The snapshot's key, which a pass records.
    key: str
    snapshot: Snapshot
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


def select(cfg: Config, selector: str) -> list[str]:
    """The gates a selector names: a gate's full name, or a group's name.

    A group gives its members in config order.
    """
    if selector in cfg.gates:
        return [selector]
    members = gate_groups(cfg).get(selector)
    if members is None:
        known = ", ".join([*cfg.gates, *gate_groups(cfg)]) or "(none)"
        raise GateError(f"no gate or group `{selector}` in config (configured: {known})")
    return members


def select_all(cfg: Config, selectors: list[str]) -> list[str]:
    """Every gate the selectors name, once each, in config order."""
    chosen = {name for selector in selectors for name in select(cfg, selector)}
    return [name for name in cfg.gates if name in chosen]


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


def snapshot(head: str | None = None) -> Snapshot:
    """The content to key: the working tree when `head` is None, otherwise the commit `head` names."""
    ref = head or "HEAD"
    try:
        commit = git.rev_parse(ref)
    except git.GitError as e:
        raise GateError(f"`{ref}` does not name a commit ({e})") from e
    if head is not None:
        return Snapshot(commit, git.rev_parse_tree(commit), False)
    try:
        tree = git.worktree_tree()
    except git.GitError as e:
        raise GateError(f"cannot key the working tree: {e}") from e
    return Snapshot(commit, tree, tree != git.rev_parse_tree(commit))


def key(cfg: Config, name: str, tree: str) -> str:
    """The gate's key for `tree`: a hash of its raw commands, its `env`, and its scope's content.

    `tree` is any tree-ish. A commit and its tree give the same key.

    The placeholders stay unresolved, so the key reads no diff and no base: a
    record means the gate passed for the scope as it stands in that tree.

    Fails closed, because the lenient git helpers turn each of these into a
    stable key that many trees share:
      - a bad `tree` walks as an empty tree;
      - a scope entry that matches no file hashes no content.
    """
    spec = spec_for(cfg, name)
    try:
        _ = git.rev_parse_tree(tree)
    except git.GitError as e:
        raise GateError(f"gate `{name}`: `{tree}` does not name a tree ({e})") from e
    for entry in spec.scope:
        if not scope_lines(cfg, entry_tags(cfg, entry), tree):
            raise GateError(f"gate `{name}`: the scope entry `{entry}` matches no tracked file at `{tree[:7]}`")
    lines = scope_lines(cfg, scope_tags(cfg, spec), tree)
    header = json.dumps(
        {"v": KEY_VERSION, "gate": name, "commands": spec.commands, "env": spec.env},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha1("\n".join([header, *lines]).encode()).hexdigest()


def uncommitted_in_scope(cfg: Config, name: str, snap: Snapshot) -> list[str]:
    """The uncommitted changes and untracked files in `snap` that touch the gate's scope."""
    spec = spec_for(cfg, name)
    return [f for f in git.diff_names(snap.commit, snap.tree) if scope_touched(cfg, scope_tags(cfg, spec), [f])]


def comparator(
    cfg: Config,
    name: str,
    base: str,
    snap: Snapshot,
    snap_key: str,
    origin: OriginRecords | None = None,
    *,
    accept: Sequence[Accept] = (),
) -> Comparator:
    """The newest content, from the snapshot back to its merge base with `base`, that has a record.

    The snapshot comes first. A gate with no placeholder stops there: an older
    record cannot narrow its commands, so it then diffs from the merge base,
    and needs none when `base` cannot be read. A gate with placeholders walks
    the first-parent line from `snap.commit` down to the merge base, and falls
    back to the merge base with no hit. A commit whose key is the snapshot's
    was already looked up, so the walk does not look it up again: on a clean
    tree that is HEAD.
    """
    spec = spec_for(cfg, name)
    found = OriginRecords(f"{REF_PREFIX}/{name}/*") if origin is None else origin
    trail: list[Step] = []

    where, runs = lookup(name, snap_key, found, accept)
    trail.append(Step(snap.commit, snap.label, snap_key, where, runs))
    if where:
        return Comparator(snap.commit, where, f"record at {snap.label}, {where}", trail, verified=True)
    try:
        mb = git.merge_base(base, snap.commit)
    except git.GitError as e:
        if has_placeholder(spec):
            raise GateError(
                f"gate `{name}`: cannot diff `{base}` against `{snap.commit[:7]}`, so the commands cannot be "
                f"resolved. Fetch `{base}` with enough history to reach the merge base, or pass `--base` ({e})"
            ) from e
        return Comparator(None, None, f"no merge base with {base}: {e}", trail)
    if has_placeholder(spec):
        for commit in [*git.rev_list_first_parent(snap.commit, mb), mb]:
            try:
                k = key(cfg, name, commit)
            except GateError:
                # Only the snapshot must have a key. An older commit with none holds no record.
                trail.append(Step(commit, commit[:7], None, None))
                continue
            where, runs = (None, []) if k == snap_key else lookup(name, k, found, accept)
            trail.append(Step(commit, commit[:7], k, where, runs))
            if where:
                return Comparator(commit, where, f"record at {commit[:7]}, {where}", trail)
    return Comparator(mb, None, f"merge base with {base}", trail)


def plan(
    cfg: Config,
    name: str,
    base: str,
    snap: Snapshot,
    origin: OriginRecords | None = None,
    *,
    full: bool = False,
    accept: Sequence[Accept] = (),
) -> GatePlan:
    """What a run of the gate on the snapshot does. See `docs/specs/gate.md#status-values`.

    `verified` when the snapshot has a record. Otherwise the diff from the
    comparator to the snapshot's tree decides: `not-needed` when no key input
    changed or every placeholder list is empty, and `required` otherwise.
    `full` reads no record and no diff, and is always `required`: each list is
    complete, as if every tag changed. See `[run-full]` in `docs/specs/gate.md`.
    """
    spec = spec_for(cfg, name)
    snap_key = key(cfg, name, snap.tree)
    if full:
        lists = placeholder_lists(spec, full_answer_set(cfg, snap.tree, compute_shas=False))
        return GatePlan(
            name=name,
            status="required",
            key=snap_key,
            snapshot=snap,
            effective_base=None,
            where=None,
            how="--full",
            commands=resolve_commands(spec, lists),
            reason="--full",
            trail=[],
            changed=[],
            lists=lists,
        )
    comp = comparator(cfg, name, base, snap, snap_key, origin, accept=accept)
    changed: list[str] = []
    lists: dict[str, list[str]] = {}

    ignored = sum(1 for step in comp.trail if step.runs and not step.where)

    def made(status: str, reason: str, commands: list[list[str]]) -> GatePlan:
        if ignored:
            reason += f" ({ignored} record(s) ignored by --accept)"
        return GatePlan(
            name=name,
            status=status,
            key=snap_key,
            snapshot=snap,
            effective_base=comp.commit,
            where=comp.where,
            how=comp.how,
            commands=commands,
            reason=reason,
            trail=comp.trail,
            changed=changed,
            lists=lists,
        )

    if comp.verified:
        return made("verified", f"{comp.where} record {snap_key}", [])
    if comp.commit is None:
        print(f"warning: gate `{name}`: {comp.how}, so it counts as needed", file=sys.stderr)
        return made("required", comp.how, [list(argv) for argv in spec.commands])

    # The comparator is an ancestor of `snap.commit`, so this two-dot diff is
    # the three-dot diff of a clean tree, plus any uncommitted work.
    files = git.diff_names(comp.commit, snap.tree)
    changed = [f for f in files if scope_touched(cfg, scope_tags(cfg, spec), [f])]
    lists = (
        placeholder_lists(spec, answer_set_for_files(cfg, files, snap.tree, compute_shas=False))
        if has_placeholder(spec)
        else {}
    )
    commands = resolve_commands(spec, lists)
    if not changed:
        return made("not-needed", f"no key input changed since {comp.how}", commands)
    if lists and not any(lists.values()):
        return made("not-needed", "every placeholder list is empty", commands)
    return made("required", f"{len(changed)} key input(s) changed since {comp.how}", commands)


def ref_for(name: str, key: str) -> str:
    return f"{REF_PREFIX}/{name}/{key}"


def write_record(plan: GatePlan, runner: Mapping[str, str], duration: float) -> str:
    """Record a pass of the planned gate, run on `runner` in `duration` seconds, in the local ref. Returns the ref.

    The run joins the runs already at the ref. A local ref that is not a
    readable record is replaced, with a warning.
    """
    run: dict[str, object] = {
        "head": plan.snapshot.commit,
        "tree": plan.snapshot.tree,
        "dirty": plan.snapshot.dirty,
        "base": plan.effective_base,
        "user": git.config_get("user.email") or "unknown",
        "time": now().isoformat(timespec="seconds"),
        "duration": round(duration, 1),
        "tangier": __version__,
        "commands": plan.commands,
        "runner": dict(runner),
    }
    ref = ref_for(plan.name, plan.key)
    runs: list[dict[str, object]] = []
    if git.ref_exists(ref):
        try:
            runs = read_runs(git.rev_parse_ref(ref))
        except GateError as e:
            print(f"warning: replaced {ref}: {e}", file=sys.stderr)
    git.update_ref(ref, _write_blob(plan.name, plan.key, merge_runs(runs, [run])))
    return ref


def _write_blob(name: str, key: str, runs: list[dict[str, object]]) -> str:
    record = {"format": RECORD_FORMAT, "gate": name, "key": key, "runs": runs}
    return git.hash_object(json.dumps(record, indent=2, sort_keys=True) + "\n")


def merge_runs(*lists: list[dict[str, object]]) -> list[dict[str, object]]:
    """The runs of every list, oldest first, without repeats, and at most `MAX_RUNS` of the newest.

    Two runs are the same run when their `time`, `head` and `runner` match.
    """
    seen: dict[str, dict[str, object]] = {}
    for run in (run for runs in lists for run in runs):
        ident = json.dumps([run.get("time"), run.get("head"), run.get("runner")], sort_keys=True)
        seen.setdefault(ident, run)
    return sorted(seen.values(), key=lambda run: str(run.get("time", "")))[-MAX_RUNS:]


def read_record(sha: str) -> dict[str, object]:
    """The record a gate ref points at. Raises `GateError` when it is not one."""
    try:
        record = json.loads(git.cat_blob(sha))
    except (git.GitError, ValueError) as e:
        raise GateError(f"not a gate record: {e}") from e
    if not isinstance(record, dict):
        raise GateError("not a gate record: the blob is not a JSON object")
    return record


def read_runs(sha: str) -> list[dict[str, object]]:
    """The runs in the record a gate ref points at. Raises `GateError` when it is not one.

    A legacy blob, with no `runs`, is one `local` run. See `docs/specs/gate.md#store`.
    A newer format than this tangier writes is not one.
    """
    record = read_record(sha)
    version = record.get("format", RECORD_FORMAT)
    if not isinstance(version, int) or version > RECORD_FORMAT:
        raise GateError(f"record format {version} is newer than this tangier reads ({RECORD_FORMAT})")
    if "runs" not in record:
        legacy = {name: value for name, value in record.items() if name not in ("gate", "key")}
        return [{**legacy, "runner": {"kind": "local"}}]
    runs = record["runs"]
    if not isinstance(runs, list) or not all(
        isinstance(run, dict) and isinstance(run.get("runner"), dict) for run in runs
    ):
        raise GateError("not a gate record: `runs` is not a list of runs, each with a `runner`")
    return runs


def _mirror(ref: str) -> str:
    """Where `fetch_origin` puts origin's `ref`, a ref or a pattern under `REF_PREFIX`."""
    return ORIGIN_MIRROR_PREFIX + ref[len(REF_PREFIX) :]


def fetch_origin(pattern: str) -> None:
    """Mirror origin's gate refs that match `pattern` under `ORIGIN_MIRROR_PREFIX`.

    The fetch prunes, so a mirrored ref that origin no longer has goes too.
    """
    git.fetch(REMOTE, f"+{pattern}:{_mirror(pattern)}")


class OriginRecords:
    """Origin's gate records that match `pattern`, fetched in one call, at the first lookup.

    A caller that finds every record locally then reads no network. An origin
    that cannot be reached counts as holding no record, with a warning: the
    caller then runs the gate, which is the safe direction.
    """

    def __init__(self, pattern: str = f"{REF_PREFIX}/*") -> None:
        self.pattern = pattern
        self._blobs: dict[str, str] | None = None
        self._runs: dict[str, list[dict[str, object]]] = {}

    def _fetched(self) -> dict[str, str]:
        """Each record's ref on origin, mapped to its blob."""
        if self._blobs is None:
            try:
                fetch_origin(self.pattern)
                found = git.for_each_ref(_mirror(self.pattern).removesuffix("/*"))
            except git.GitError as e:
                print(f"warning: cannot read gate records from {REMOTE}, so they count as absent: {e}", file=sys.stderr)
                found = []
            self._blobs = {REF_PREFIX + ref[len(ORIGIN_MIRROR_PREFIX) :]: sha for sha, ref in found}
        return self._blobs

    def __contains__(self, ref: object) -> bool:
        return ref in self._fetched()

    def runs(self, ref: str) -> list[dict[str, object]]:
        """The runs in origin's record at `ref`. Empty, with a warning, when it cannot be read."""
        if ref not in self._runs:
            try:
                self._runs[ref] = read_runs(self._fetched()[ref])
            except GateError as e:
                print(f"warning: cannot read {ref} from {REMOTE}, so it counts as absent: {e}", file=sys.stderr)
                self._runs[ref] = []
        return self._runs[ref]


def lookup(
    name: str, key: str, origin: OriginRecords | None = None, accept: Sequence[Accept] = ()
) -> tuple[str | None, list[tuple[dict[str, object], bool]]]:
    """Where an accepted run for this key is, `local`, `origin` or None, and every run read on the way.

    Local first, since it needs no network. `origin` is shared by a caller
    that checks several keys, so origin is read once. A record with no
    accepted run is a miss, as if it were not there, and so is one that cannot
    be read.
    """
    ref = ref_for(name, key)
    seen: list[tuple[dict[str, object], bool]] = []
    if git.ref_exists(ref):
        try:
            seen += [(run, accepted(run, accept)) for run in read_runs(git.rev_parse_ref(ref))]
        except GateError as e:
            print(f"warning: {ref} counts as absent: {e}", file=sys.stderr)
        if any(ok for _, ok in seen):
            return "local", seen
    found = OriginRecords(f"{REF_PREFIX}/{name}/*") if origin is None else origin
    if ref in found:
        runs = [(run, accepted(run, accept)) for run in found.runs(ref)]
        # After a push, origin holds the local runs too.
        seen += [entry for entry in runs if entry not in seen]
        if any(ok for _, ok in runs):
            return "origin", seen
    return None, seen


def verified(name: str, key: str, origin: OriginRecords | None = None, accept: Sequence[Accept] = ()) -> str | None:
    """Where an accepted run for this key is: `local`, `origin`, or None. See `lookup`."""
    return lookup(name, key, origin, accept)[0]


# How many fetch, merge and push attempts `publish` and `sync` make before they give up on a racing origin.
SYNC_ATTEMPTS = 3


@dataclass
class Synced:
    """How many gate refs a publish or sync pushed as they were, pulled from origin, merged and pushed, and pruned.

    Each ref counts once.
    """

    pushed: int
    pulled: int
    merged: int
    pruned: int


def publish(refs: set[str]) -> Synced:
    """Pull origin's gate records, then push only `refs`: the records a `gate run` wrote.

    The first attempt pulls every record. A rejected push retries on the gates
    of `refs` alone, up to `SYNC_ATTEMPTS` times. See `docs/specs/gate.md#store`.
    """
    before = _local_refs()
    patterns = [f"{REF_PREFIX}/*"]
    for _ in range(SYNC_ATTEMPTS):
        leases: dict[str, str] = {}
        for pattern in patterns:
            leases.update(_reconcile(pattern, None)[0])
        wanted = {ref: sha for ref, sha in leases.items() if ref in refs}
        try:
            _push(wanted, set())
        except git.PushRejected:
            # A pattern, not the exact ref: a fetch of an exact ref that origin lacks fails.
            patterns = sorted({ref.rpartition("/")[0] + "/*" for ref in refs})
            continue
        return _counted(before, set(wanted), set())
    raise GateError(f"{REMOTE}'s gate records kept changing")


def sync(prune_after_days: int) -> Synced:
    """Sync every gate record with origin: pull, merge runs, push what changed, and prune expired records.

    See `docs/specs/gate.md#store`.
    """
    before = _local_refs()
    pruned: set[str] = set()
    for _ in range(SYNC_ATTEMPTS):
        leases, expired = _reconcile(f"{REF_PREFIX}/*", now() - timedelta(days=prune_after_days))
        pruned |= expired
        try:
            _push(leases, expired)
        except git.PushRejected:
            continue
        # A retry can pull back a record an earlier attempt deleted, because origin's copy gained a fresh run.
        return _counted(before, set(leases), pruned - _local_refs().keys())
    raise GateError(f"{REMOTE}'s gate records kept changing; run `gate sync` again")


def _push(leases: dict[str, str], deletes: set[str]) -> None:
    """One leased, atomic push of each ref in `leases`: the local record, or a delete for a ref in `deletes`."""
    if leases:
        refspecs = [f":{ref}" if ref in deletes else f"{ref}:{ref}" for ref in leases]
        git.push(REMOTE, refspecs, leases=leases, atomic=True)


def _counted(before: dict[str, str], pushed: set[str], pruned: set[str]) -> Synced:
    # A ref whose local record changed took runs from origin. A retry can change it more than once.
    after = _local_refs()
    changed = {ref for ref in before.keys() | after.keys() if after.get(ref) != before.get(ref)} - pruned
    pushed -= pruned
    return Synced(
        pushed=len(pushed - changed), pulled=len(changed - pushed), merged=len(pushed & changed), pruned=len(pruned)
    )


def _local_refs() -> dict[str, str]:
    return {ref: sha for sha, ref in git.for_each_ref(REF_PREFIX)}


def _reconcile(pattern: str, cutoff: datetime | None) -> tuple[dict[str, str], set[str]]:
    """Fetch origin's refs matching `pattern` and resolve each against the local ref.

    Writes the resolved record locally: pulled, merged, or deleted when its
    newest run is older than `cutoff`. Returns each ref whose resolved state
    differs from origin's, mapped to the SHA origin held ("" if absent): the
    push to make. Also returns the refs it found expired, whose push is a
    delete. A record only this clone holds that has expired is dropped, not
    pushed.
    """
    fetch_origin(pattern)
    prefix = pattern.removesuffix("/*")
    mine = {ref: sha for sha, ref in git.for_each_ref(prefix)}
    theirs = {REF_PREFIX + ref[len(ORIGIN_MIRROR_PREFIX) :]: sha for sha, ref in git.for_each_ref(_mirror(prefix))}
    leases: dict[str, str] = {}
    expired: set[str] = set()
    for ref in sorted(mine.keys() | theirs.keys()):
        sha, other = mine.get(ref), theirs.get(ref)
        if sha == other and cutoff is None:
            continue
        result = _reconciled(ref, sha, other) if sha and other and sha != other else sha or other or ""
        if cutoff is not None and _expired(ref, result, cutoff):
            if sha is not None:
                git.delete_ref(ref)
            # Leased on origin's SHA, so a record that gained a fresh run since the fetch is not deleted.
            if other is not None:
                leases[ref] = other
            expired.add(ref)
            continue
        if result != sha:
            git.update_ref(ref, result)
        if result != other:
            leases[ref] = other or ""
    return leases, expired


def _expired(ref: str, sha: str, cutoff: datetime) -> bool:
    """Whether the record's newest run is older than `cutoff`.

    Age is the run's own `time`, not a ref or commit date: a record is a blob
    and carries no other date. A record that cannot be read is kept, with a
    warning.
    """
    try:
        return max(datetime.fromisoformat(str(run["time"])) for run in read_runs(sha)) < cutoff
    except (GateError, KeyError, ValueError, TypeError) as e:
        print(f"warning: skipped {ref}: {e}", file=sys.stderr)
        return False


def _reconciled(ref: str, sha: str, other: str) -> str:
    """The record to keep at `ref`, given the local record `sha` and origin's `other`: both their runs.

    An unreadable local record yields origin's, so it never overwrites a readable
    one. An unreadable origin record yields the local one, which then replaces it.
    """
    try:
        mine = read_runs(sha)
    except GateError as e:
        print(f"warning: kept {REMOTE}'s {ref}, because the local one is unreadable: {e}", file=sys.stderr)
        return other
    try:
        runs = merge_runs(mine, read_runs(other))
    except GateError as e:
        print(f"warning: {REMOTE}'s {ref} is unreadable, so the local one replaces it: {e}", file=sys.stderr)
        return sha
    name, _, key = ref[len(REF_PREFIX) + 1 :].rpartition("/")
    return _write_blob(name, key, runs)
