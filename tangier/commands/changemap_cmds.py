"""`tangier changemap ...` — formatters over the answer set.

Every output shape here is parsed by something downstream (a workflow's
`$GITHUB_OUTPUT`, `export $(...)`, a `paste -sd,` pipeline), so the formats are
contracts. `bin/parity-check` diffs them against the pre-extraction script.
"""

from __future__ import annotations

import argparse
import json
import sys

from tangier import git
from tangier.changemap import (
    buckets,
    compute_answer_set,
    expand_with_dependents,
    match_files_to_tags,
    project_items,
    provenance,
    sha_for_bucket,
)
from tangier.config import Config, version_var_for
from tangier.github import emit_outputs


def cmd_list(config: Config, args: argparse.Namespace) -> int:
    if getattr(args, "graph", False):
        for tag in sorted(config.paths.keys()):
            print(tag)
            for dep in sorted(config.depends.get(tag, [])):
                print(f"  {dep}")
        return 0
    for tag in sorted(config.paths.keys()):
        print(tag)
        for g in config.paths[tag]:
            print(f"  {g}")
    return 0


def cmd_sha(config: Config, args: argparse.Namespace) -> int:
    bucket_map = buckets(config)
    # `--all` deliberately beats a positional bucket rather than erroring —
    # preserved from the pre-extraction behaviour.
    if args.bucket and not args.all:
        if args.bucket not in bucket_map:
            print(f"unknown bucket: {args.bucket}", file=sys.stderr)
            return 2
        print(sha_for_bucket(config, args.bucket, args.head))
        return 0
    versions = {b: sha_for_bucket(config, b, args.head) for b in sorted(bucket_map.keys())}
    if args.github_notice:
        # Markdown table; skip *-base buckets per legacy convention.
        table = {k: v for k, v in versions.items() if not k.endswith("-base")}
        print("## Package builds\n")
        print("| Package | Build ID |")
        print("|-----|-------|")
        for k, v in table.items():
            print(f"| {k} | {v} |")
    else:
        for k, v in versions.items():
            print(f"{version_var_for(k)}={v}")
    return 0


def cmd_items(config: Config, args: argparse.Namespace) -> int:
    if args.name not in config.items:
        # Unknown items name -> empty output, exit 0. A typo in a runner script
        # yields "nothing changed" rather than a failure; preserved deliberately.
        return 0
    answers = compute_answer_set(config, args.base, args.head, expand=not args.no_expand, compute_shas=False)
    for path in answers.items.get(args.name, []):
        print(path)
    return 0


def cmd_list_ignored(config: Config, args: argparse.Namespace) -> int:
    files = git.changed_files(args.base, args.head)
    _per_tag, ignored = match_files_to_tags(config, files)
    for p in sorted(ignored):
        print(p)
    return 0


def parse_files_args(pairs: list[str] | None) -> dict[str, str]:
    """Parse repeatable `--files GROUP=CSV` into {group: csv}.

    An empty csv is legal (it means "no files in this group"). A pair with no
    `=` is a usage error, raising ValueError for the CLI to turn into exit 2.
    """
    out: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"--files expects GROUP=CSV, got: {pair}")
        group, _, csv = pair.partition("=")
        if not group:
            raise ValueError(f"--files expects a non-empty group name, got: {pair}")
        out[group] = csv
    return out


def cmd_explain(config: Config, args: argparse.Namespace) -> int:
    """Render the answer set as markdown for humans / $GITHUB_STEP_SUMMARY.

    Three sections: modified tags with their files, dependent tags pulled in via
    depends, and the runner invocation lines CI would execute.
    """
    files_by_group = getattr(args, "files_map", None)
    if files_by_group is None:
        files_by_group = parse_files_args(getattr(args, "files", None))

    files = git.changed_files(args.base, args.head)
    per_tag, _ignored = match_files_to_tags(config, files)
    matched_raw = set(per_tag.keys())
    expanded = matched_raw if args.no_expand else expand_with_dependents(matched_raw, config.depends)
    prov = provenance(matched_raw, expanded, config.depends)

    print("## Modified tags")
    print("")
    if not matched_raw:
        print("(none — no changed file matched any tag's paths)")
    else:
        first = True
        for tag in sorted(matched_raw):
            if not first:
                print("")
            first = False
            print(f"{tag} (matched directly):")
            for p in sorted(per_tag[tag]):
                print(f"- {p}")

    if not args.no_expand:
        dependents = expanded - matched_raw
        if dependents:
            print("")
            print("## Dependent tags (run because they depend on a modified tag)")
            print("")
            for tag in sorted(dependents):
                sources = ", ".join(sorted(prov.get(tag, set())))
                print(f"{tag} (depends on: {sources})")

    print("")
    print("## Resulting CI invocations")
    print("")
    # The literal two-character `""` is the sentinel for an empty dir list —
    # a shell-safe empty argument, not a blank.
    empty_sentinel = '""'
    for name in sorted(config.items.keys()):
        paths = project_items(config, name, expanded)
        csv = ",".join(paths) if paths else empty_sentinel
        runner = config.runners.get(name)
        if runner is not None:
            line = f"{runner.cmd} --dirs {csv}"
            # `files` names the file-set group, which is NOT derivable from the
            # items name: items `unittest` pairs with group `unittest-files`.
            if runner.files:
                selected = files_by_group.get(runner.files, "")
                if selected:
                    line += f" --files {selected}"
            print(line)
        else:
            print(f"# {name}: {csv}")
    return 0


def cmd_build_matrix(config: Config, args: argparse.Namespace) -> int:
    """Emit the buildable buckets this diff touches, as a JSON array for a matrix.

    A separate command rather than a line in `github-outputs`, whose contract is
    that output names are derived mechanically from config — a hardcoded
    `build-packages` key would break that rule.

    The set is BUCKETS, deduped, not tags: `sha_bucket` is many-to-one, so a
    tag-level intersection would try to build the same bucket twice.

    Built from `expanded`, not `matched`: a bucket reached only through
    `depends` must still rebuild — that is the premise of content-addressed
    tagging, since its hash has moved.

    `build-packages-empty` exists because `strategy.matrix` over `[]` is a hard
    error in Actions, not a skip, and a boolean an `if:` can read is far easier
    to get right than `fromJSON(...)[0]`.
    """
    # No hashes: the matrix names buckets, and each leg computes its own tag.
    answers = compute_answer_set(config, args.base, args.head, expand=not args.no_expand, compute_shas=False)
    touched = {config.sha_bucket[t] for t in answers.expanded if t in config.sha_bucket}
    packages = sorted(touched & set(config.images))
    emit_outputs(
        {
            "build-packages": json.dumps(packages, separators=(",", ":")),
            "build-packages-empty": "true" if not packages else "false",
        }
    )
    return 0


def cmd_github_outputs(config: Config, args: argparse.Namespace) -> int:
    """Emit the answer set's CI-relevant fields.

    Always echoes to stdout; appends to $GITHUB_OUTPUT when set. Output names
    are derived from the config: `<bucket>-sha`, `<name>-items`, `<tag>-touched`,
    and one line per file-set group whose name IS the output name (so adding an
    `[e2e-files]` table needs no code change here).
    """
    answers = compute_answer_set(config, args.base, args.head, expand=not args.no_expand)
    # Insertion order is the emitted order, and `bin/parity-check` diffs it
    # against the pre-extraction script — so the four groups stay in this order.
    pairs: dict[str, str] = {}
    for bucket in sorted(answers.shas):
        pairs[f"{bucket}-sha"] = answers.shas[bucket]
    for name in sorted(answers.items):
        pairs[f"{name}-items"] = ",".join(answers.items[name])
    for tag in sorted(answers.touched):
        pairs[f"{tag}-touched"] = "true" if answers.touched[tag] else "false"
    for group in sorted(answers.file_sets):
        pairs[group] = ",".join(answers.file_sets[group])

    emit_outputs(pairs)
    return 0
