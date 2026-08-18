"""The answer set: which tags does this diff touch, and what are their SHAs?

The single computed view that every `changemap` subcommand formats. Nothing
here formats output or shells out to a cluster.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from tangier import git, globs
from tangier.config import Config

# `from tangier import git` — NOT `from tangier.git import changed_files`. Tests
# patch `git.changed_files`, and a direct-symbol import would bind a second
# reference that the patch never reaches.


@dataclass
class AnswerSet:
    matched: set[str]
    expanded: set[str]
    shas: dict[str, str]
    items: dict[str, list[str]]
    touched: dict[str, bool]
    # File-set group name (e.g. `unittest-files`) -> directly-changed files
    # selected by that group's globs. Drives each runner's `--files` flag.
    file_sets: dict[str, list[str]]
    ignored: list[str]


def tag_matches(cfg: Config, tag: str, path: str) -> bool:
    """Whether `path` belongs to `tag`: matches its `paths`, minus its `exclude`.

    The single definition of tag membership — every match site (diff resolution
    and SHA bucket hashing) goes through here so the two cannot diverge.
    """
    return globs.matches(path, cfg.paths.get(tag, [])) and not globs.matches(path, cfg.exclude.get(tag, []))


# ---------------------------------------------------------------------------
# SHA — content hash for a bucket
# ---------------------------------------------------------------------------


def sha_of_paths(paths: list[str], head: str, keep: Callable[[str], bool] | None = None) -> str:
    """Hash the `git ls-tree -r` walk of `paths`, optionally filtered by `keep`.

    `keep` receives each walked file path and decides whether it contributes —
    the only way to express exclusion, since `ls-tree` has no negation.
    `keep=None` skips the per-path call entirely; it is an optimisation, not a
    parity mechanism, since a `keep` returning True for every line hashes
    identically.

    What byte-identical hashing actually rests on is here and in
    `globs.globs_to_ls_tree_paths`: the exact `ls-tree` line text, joined with
    `\\n` and hashed with no trailing newline, truncated to 10 hex chars. Change
    any of that and every image tag in every consuming repo moves.

    Policy-free by design — the caller composes `keep`, so `[sha] exclude` and
    per-tag `exclude` meet in one place rather than two.
    """
    h = hashlib.sha1()
    lines = git.ls_tree(head, paths)
    filtered = [line for line in lines if keep is None or keep(git.ls_tree_path(line))]
    h.update("\n".join(filtered).encode())
    return h.hexdigest()[:10]


def transitive_deps(tag: str, depends: dict[str, list[str]]) -> set[str]:
    """Transitive closure of `tag`'s forward dependencies (what `tag` depends on)."""
    seen: set[str] = set()
    queue = [tag]
    while queue:
        cur = queue.pop()
        for dep in depends.get(cur, ()):
            if dep not in seen:
                seen.add(dep)
                queue.append(dep)
    return seen


def buckets(cfg: Config) -> dict[str, list[str]]:
    """bucket-name -> member tags (tags whose `sha` field maps here)."""
    out: dict[str, list[str]] = {}
    for tag, bucket in cfg.sha_bucket.items():
        out.setdefault(bucket, []).append(tag)
    return out


def sha_for_bucket(cfg: Config, bucket: str, head: str = "HEAD") -> str:
    """SHA of the union of a bucket's members' paths + their transitive deps' paths.

    Contributing tags' `exclude` globs are honoured so a bucket's contents match
    the tags' match sets. `git ls-tree` cannot express negation, so the file list
    it returns is filtered afterwards. Exclusion is a property of the individual
    tag, not the union: a path one contributing tag excludes still counts if
    another contributing tag claims it (that tag genuinely ships the file).

    Two filters compose here, and they are deliberately different:
      - `[sha] exclude` applies unconditionally (docs never rebuild an image).
      - the per-tag `claimed_by` filter applies only when some contributing tag
        actually declares `exclude`.
    Conflating them would change the hash of every bucket that has no exclusions.
    """
    members = buckets(cfg).get(bucket, [])
    bucket_globs: list[str] = []
    contributing: set[str] = set()
    for m in members:
        # Set iteration, so the order of `bucket_globs` — and hence of the path
        # arguments to `git ls-tree` — is not stable across runs. That is safe
        # only because `git ls-tree -r` sorts and dedups its output regardless
        # of argument order; the SHA depends on the output, not the arguments.
        for tag in {m, *transitive_deps(m, cfg.depends)}:
            if tag in contributing:
                continue
            contributing.add(tag)
            bucket_globs.extend(cfg.paths.get(tag, []))

    sha_exclude = cfg.sha.exclude
    needs_claim_filter = any(cfg.exclude.get(tag) for tag in contributing)

    def _keep(path: str) -> bool:
        if sha_exclude and globs.matches(path, sha_exclude):
            return False
        if needs_claim_filter:
            return any(tag_matches(cfg, tag, path) for tag in contributing)
        return True

    # Neither filter applies -> walk everything unfiltered, the byte-stable fast path.
    keep: Callable[[str], bool] | None = _keep if (sha_exclude or needs_claim_filter) else None
    return sha_of_paths(globs.globs_to_ls_tree_paths(bucket_globs), head, keep)


# ---------------------------------------------------------------------------
# Dependency expansion
# ---------------------------------------------------------------------------


def _invert_depends(depends: dict[str, list[str]]) -> dict[str, set[str]]:
    """Build dep -> {tags that directly depend on dep}."""
    out: dict[str, set[str]] = {}
    for tag, deps in depends.items():
        for dep in deps:
            out.setdefault(dep, set()).add(tag)
    return out


def expand_with_dependents(tags: Iterable[str], depends: dict[str, list[str]]) -> set[str]:
    """Expand a matched tag set via the reverse-transitive closure of depends.

    For each input tag X, union in every tag whose dependency closure
    transitively contains X.
    """
    inverse = _invert_depends(depends)
    result: set[str] = set(tags)
    queue: list[str] = list(tags)
    while queue:
        cur = queue.pop()
        for dependent in inverse.get(cur, ()):
            if dependent not in result:
                result.add(dependent)
                queue.append(dependent)
    return result


def provenance(matched_raw: set[str], expanded: set[str], depends: dict[str, list[str]]) -> dict[str, set[str]]:
    """For each tag in `expanded - matched_raw`, the matched-raw tags whose
    dependents transitively include it."""
    added = expanded - matched_raw
    out: dict[str, set[str]] = {}
    for tag in added:
        sources = {src for src in matched_raw if tag in expand_with_dependents({src}, depends)}
        if sources:
            out[tag] = sources
    return out


# ---------------------------------------------------------------------------
# Answer set
# ---------------------------------------------------------------------------


def match_files_to_tags(cfg: Config, files: Iterable[str]) -> tuple[dict[str, list[str]], list[str]]:
    """Group files by the tag(s) they match. Returns (per_tag, ignored)."""
    per_tag: dict[str, list[str]] = {}
    ignored: list[str] = []
    for f in files:
        hit = False
        for tag in cfg.paths:
            if tag_matches(cfg, tag, f):
                per_tag.setdefault(tag, []).append(f)
                hit = True
        if not hit:
            ignored.append(f)
    return per_tag, ignored


def project_items(cfg: Config, name: str, expanded: set[str]) -> list[str]:
    """Stable, deduped projection of one items name through the expanded set."""
    tag_map = cfg.items.get(name, {})
    seen: set[str] = set()
    collected: list[str] = []
    for tag in sorted(expanded):
        for path in tag_map.get(tag, []):
            if path not in seen:
                seen.add(path)
                collected.append(path)
    return collected


def compute_answer_set(
    cfg: Config, base: str, head: str, *, expand: bool = True, compute_shas: bool = True
) -> AnswerSet:
    files = git.changed_files(base, head)
    per_tag, ignored = match_files_to_tags(cfg, files)
    matched = set(per_tag.keys())
    expanded = expand_with_dependents(matched, cfg.depends) if expand else set(matched)

    items = {name: project_items(cfg, name, expanded) for name in cfg.items}

    shas: dict[str, str] = {}
    if compute_shas:
        for bucket in sorted(buckets(cfg).keys()):
            shas[bucket] = sha_for_bucket(cfg, bucket, head)

    touched = {tag: (tag in expanded) for tag in cfg.touched}

    # File-set projection: each `files = true` table's globs select the
    # directly-changed files for a runner's `--files` flag. Membership is decided
    # purely by the table's globs over the raw diff — scoped to the code each
    # runner can actually execute. Files outside every file-set's globs silently
    # drop out, the same ignore-by-default contract as the rest of changemap.
    file_sets = {
        group: [f for f in files if globs.matches(f, group_globs)] for group, group_globs in cfg.file_sets.items()
    }

    return AnswerSet(
        matched=matched,
        expanded=expanded,
        shas=shas,
        items=items,
        touched=touched,
        file_sets=file_sets,
        ignored=ignored,
    )
