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
KEY_VERSION = 1


class GateError(RuntimeError):
    """A gate cannot be keyed, run or recorded."""


@dataclass
class Resolved:
    """A gate at one (base, head): its key, and the commands the key covers."""

    key: str
    commands: list[list[str]]
    # The commit `head` named when the key was made.
    head: str


def now() -> datetime:
    """The record clock. Tests patch this."""
    return datetime.now(UTC)


def spec_for(cfg: Config, name: str) -> GateSpec:
    spec = cfg.gates.get(name)
    if spec is None:
        known = ", ".join(sorted(cfg.gates)) or "(none)"
        raise GateError(f"no `[gate.{name}]` in config (configured: {known})")
    return spec


def resolve_commands(gate: GateSpec, answers: AnswerSet) -> list[list[str]]:
    """Replace each placeholder token with its comma-joined list.

    An empty list gives an empty argument, so the argv shape does not depend on
    the diff. The parser has already checked each placeholder against the
    config, so a lookup here cannot miss.
    """

    def substitute(token: str) -> str:
        if not is_placeholder(token):
            return token
        name = token[1:-1]
        if name.endswith(ITEMS_PLACEHOLDER_SUFFIX):
            return ",".join(answers.items[name[: -len(ITEMS_PLACEHOLDER_SUFFIX)]])
        return ",".join(answers.file_sets[name])

    return [[substitute(token) for token in argv] for argv in gate.commands]


def has_placeholder(spec: GateSpec) -> bool:
    return any(is_placeholder(token) for argv in spec.commands for token in argv)


def needed(cfg: Config, name: str, base: str, head: str) -> bool:
    """Whether the diff from `base` to `head` needs this gate. See `docs/specs/gate.md#need`.

    It does when a changed file is one of the scope's key inputs and, for a
    gate with placeholders, at least one placeholder list is non-empty. A
    `base` with no merge base cannot be diffed, so the gate is needed: running
    it is the safe direction.
    """
    spec = spec_for(cfg, name)
    try:
        _ = git.merge_base(base, head)
    except git.GitError as e:
        print(
            f"warning: gate `{name}`: cannot diff `{base}` against `{head}`, so it counts as needed: {e}",
            file=sys.stderr,
        )
        return True
    if not scope_touched(cfg, scope_tags(cfg, spec), git.changed_files(base, head)):
        return False
    if not has_placeholder(spec):
        return True
    resolved = resolve_commands(spec, compute_answer_set(cfg, base, head, compute_shas=False))
    return any(
        is_placeholder(token) and arg
        for argv, resolved_argv in zip(spec.commands, resolved, strict=True)
        for token, arg in zip(argv, resolved_argv, strict=True)
    )


def _commands_for(cfg: Config, name: str, spec: GateSpec, base: str, head: str) -> list[list[str]]:
    """The gate's commands for this diff, with item and file lists filled in.

    A gate with no placeholder takes nothing from the diff, so `base` is not
    read: such a gate works in a shallow checkout.
    """
    if not has_placeholder(spec):
        return [list(argv) for argv in spec.commands]
    try:
        _ = git.merge_base(base, head)
    except git.GitError as e:
        raise GateError(
            f"gate `{name}`: cannot diff `{base}` against `{head}`, so the commands cannot be resolved. "
            f"Fetch `{base}` with enough history to reach the merge base, or pass `--base` ({e})"
        ) from e
    return resolve_commands(spec, compute_answer_set(cfg, base, head, compute_shas=False))


def resolve(cfg: Config, name: str, base: str, head: str) -> Resolved:
    """The gate's key and commands at `head`.

    Fails closed, because the lenient git helpers turn each of these into a
    stable key that many trees share:
      - a bad `head` walks as an empty tree;
      - a bad `base`, or one with no merge base, diffs as "nothing changed", so
        every item list is empty (a gate with no placeholder reads no `base`);
      - a scope entry that matches no file hashes no content.
    """
    spec = spec_for(cfg, name)
    try:
        commit = git.rev_parse(head)
    except git.GitError as e:
        raise GateError(f"gate `{name}`: `{head}` does not name a commit ({e})") from e
    commands = _commands_for(cfg, name, spec, base, head)
    for entry in spec.scope:
        if not scope_lines(cfg, entry_tags(cfg, entry), head):
            raise GateError(f"gate `{name}`: the scope entry `{entry}` matches no tracked file at `{head}`")
    lines = scope_lines(cfg, scope_tags(cfg, spec), head)
    header = json.dumps(
        {"v": KEY_VERSION, "gate": name, "commands": commands, "env": spec.env},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha1("\n".join([header, *lines]).encode()).hexdigest()
    return Resolved(key=digest, commands=commands, head=commit)


def key(cfg: Config, name: str, base: str, head: str) -> str:
    return resolve(cfg, name, base, head).key


def ref_for(name: str, key: str) -> str:
    return f"{REF_PREFIX}/{name}/{key}"


def write_record(name: str, resolved: Resolved) -> str:
    """Record a pass of `name` as a local ref. Returns the ref."""
    record = {
        "gate": name,
        "key": resolved.key,
        "head": resolved.head,
        "user": git.config_get("user.email") or "unknown",
        "time": now().isoformat(timespec="seconds"),
        "tangier": __version__,
        "commands": resolved.commands,
    }
    ref = ref_for(name, resolved.key)
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


def verified(name: str, key: str, origin: set[str] | None = None) -> str | None:
    """Where a record for this key is: `local`, `origin`, or None.

    Local first, since it needs no network. `origin` is the result of an
    earlier `origin_records()`, for a caller that checks several gates.
    """
    ref = ref_for(name, key)
    if git.ref_exists(ref):
        return "local"
    # Membership, not "any result": `ls-remote` matches a pattern against
    # trailing path components, and only the exact ref is a record.
    if ref in (origin_records(ref) if origin is None else origin):
        return "origin"
    return None


def status(is_needed: bool, where: str | None) -> str:
    """The one word CI reads for a gate: `not-needed`, `verified` or `required`, checked in that order.

    `where` is `verified()`'s answer. Only `required` means the commands must run.
    """
    if not is_needed:
        return "not-needed"
    return "verified" if where else "required"


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
