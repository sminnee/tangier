# changemap

> `tangier changemap` is a generic tag/path tool. It reads a single per-tag TOML config
> (`pipeline.toml`) and answers "which tags own these paths?", "what is the content hash of this
> SHA bucket?", and "what does the answer set look like for this diff?". It backs both selective CI
> test execution and Docker image content hashing.

## Schema

One config (`pipeline.toml`). Each non-reserved top-level table is one tag. Tags carry:

- Configs are per-tag TOML tables; each table requires `paths`. `[toml-table-form]`
- Unknown fields on a tag table raise. `[unknown-field-raises]`
- `exclude` subtracts globs from `paths`; a match needs both. `[exclude-subtracts-from-paths]`
- `exclude` is per-tag: an excluded path still matches other tags. `[exclude-is-per-tag]`
- A tag's `exclude` also filters its SHA bucket, so match and hash agree. `[exclude-applies-to-sha]`
- `<name>_items = "dir"` (or list) declares a file-list projection for the named runner.
  `[items-field-projection]`
- `sha = true` makes the tag a SHA bucket of the same name; `sha = "name"` joins an existing bucket.
  `[sha-bucket-walk]`
- `touched = true` opts the tag into a `<tag>-touched=true/false` line in `github-outputs`.
  `[touched-opt-in]`
- `files = true` makes a table a projection-only file-set (globs select diff files for `--files`).
  `[file-set-field]`
- `depends = ["tag", ...]` declares the tag's direct dependencies. `[subsystem-depends-key]`
- `paths` and `depends` accept a bare string as shorthand for a single-element list.
  `[str-or-list-shorthand]`
- Warn at parse time when a tag has neither `paths` nor `depends` (can never trigger).
  `[warn-tag-without-input]`
- Warn at parse time when a tag has no `sha`/`touched`/`*_items` and no dependents (no CI signal).
  `[warn-tag-without-output]`

## Reserved sections

Some top-level names configure tangier rather than declaring a tag: `tags`, `sha`, `registry`,
`deploy`, `image`, `k8s`, `runners`. Reserving a name is cheap; un-reserving one after a config
author has used it as a tag is a breaking change, so all seven are reserved from the start even
where the behaviour they configure lands later.

A tag whose natural name collides with one of these is declared under `[tags.*]`. Bare and nested
tag tables are merged into one flat set before validation, so the two forms are interchangeable and
`depends` resolves across the boundary in either direction.

- Tags may be declared bare (`[mytag]`) or nested (`[tags.mytag]`). `[tags-table-nesting]`
- The same tag name in both forms raises. `[duplicate-tag-raises]`
- A malformed reserved section names the `[tags.<name>]` escape hatch in its error.
  `[reserved-section-error-names-escape]`

## SHA buckets

A bucket's SHA is `sha1[:10]` of the `git ls-tree -r` walk of the union of its member tags' paths
plus their transitive dependencies' paths. Two filters compose, and they are deliberately different:

- `[sha] exclude` applies unconditionally, so documentation changes never rebuild an image. It
  defaults to `["**/README.md"]`; an explicit `exclude = []` disables it. `[sha-exclude]`
- The per-tag `exclude` filter applies only when some contributing tag actually declares one.

Conflating them would change the hash of every bucket that has no exclusions. When neither applies
the per-path filter is skipped entirely, but that is an optimisation, not a correctness mechanism —
a filter that keeps every line hashes the same.

What byte-identical hashing rests on is the exact `ls-tree` line text, joined with `\n`, hashed with
no trailing newline and truncated to 10 hex characters — plus the `dir/**` → `dir` reduction below.
Change any of those and every image tag in every consuming repo moves. `[glob-hash-inputs]`

**`git ls-tree` does not interpret globs.** Its path arguments are literal prefixes, so a tag whose
`paths` is `src/*.py` or `**/*.md` contributes *nothing* to the walk — the bucket then hashes an
empty or truncated file list and its SHA stops moving when the source changes. Only a literal path
or a `dir/**` prefix is safe in a tag that feeds a SHA bucket. Tag *matching* uses the full glob
engine, so this affects hashing only. `[sha-bucket-globs-must-be-prefixes]`

Exclusion is a property of the individual tag, not of the union: a path one contributing tag
excludes still counts if another contributing tag claims it, because that tag genuinely ships the
file.

## Glob semantics

Not `fnmatch`, not gitignore. `**` matches any characters including `/`; `*` matches any character
except `/`; `?` matches exactly one non-`/` character. Patterns are anchored at both ends.

`**` converts to `.*`, which matches the empty string, and the separator after `**` is swallowed —
so `**/README.md` matches a root-level `README.md` as well as nested ones, and `**/*.md` covers the
whole tree. SHA parity depends on this exactly. `[glob-double-star-matches-empty]`

## Dependency graph

Each tag may declare `depends = ["tag", ...]`. Expansion is the reverse-transitive closure: when X
changes, run X plus every tag whose dependency closure transitively contains X. A leaf tag called
`global` is a convention, not a special case — tags that should re-run on any cross-cutting change
list it as a dependency.

- A match expands to every tag whose `depends` closure transitively contains it.
  `[reverse-transitive-expansion]`
- `depends` referencing a tag that doesn't exist raises a parse error. `[unknown-dependency-raises]`
- A dependency cycle (`a -> b -> a`) raises a parse error naming the cycle path. `[cycle-raises]`
- A change to a "global" leaf naturally expands to every dependent — no special-case code.
  `[global-as-dependency]`

## Ignore-by-default

Resolution treats unmapped paths as ignored, not as cause for alarm — the TOML is an opt-in list of
paths that matter to selective CI, not an exhaustive map of the repo. Non-selective paths (main
branch builds, a plain full test run) still run everything, so a missing tag entry costs at most a
wasted selective-CI run, not test coverage.

- Changed files matching no tag silently drop out of the resolved tag set (no warning, no fallback).
  `[unmapped-paths-silently-ignored]`

## CLI

The CLI is formatter-only: every subcommand projects the same underlying answer set (matched,
expanded, shas, items, touched, file-sets, ignored).

- `sha <bucket>` — SHA1[:10] of the union of bucket members' paths + their transitive deps' paths.
- `sha --all` — one `<BUCKET>_VERSION=<sha>` line per bucket. This `KEY=value` shape is what
  `export $(tangier changemap sha --all)` relies on, and the derived variable names are what k8s
  manifests reference. `[version-var-derivation]`
- `sha --all --github-notice` — a markdown build table. Buckets ending `-base` are skipped, a
  legacy convention. `[github-notice-skips-base-buckets]`
- `items <name>` — one path per line for tags in the expanded set with that `_items` field. An
  unknown items name exits 0 with no output, so a typo in a runner script yields "nothing changed"
  rather than a failure. `[unknown-items-name-is-quiet]`
- `list-ignored` — paths in the diff that matched no tag.
- `github-outputs` — full answer set as `name=value` lines, echoed to stdout and appended to
  `$GITHUB_OUTPUT` when set.
- `explain` — modified tags, dependents, and the resulting runner invocations.

- `github-outputs` emits SHAs, items (csv), touched flags, and one `<group>=` line per file-set.
  The file-set group name IS the output name — no suffix is added, so a new file-set table needs no
  code change. `[github-outputs-answer-set]`
- `explain` groups changed files by tag, lists dependents, renders the runner invocation lines.
  An empty dir list renders as the literal two-character `""`, a shell-safe empty argument.
  `[explain-groups-by-tag]`
- `--no-expand` returns the un-expanded matched set (debug). `[no-expand-debug-flag]`

A bad ref yields an empty diff rather than an error: `git diff` runs with `check=False`, so a
shallow clone missing `origin/main` degrades to "nothing changed" instead of failing CI.
`[bad-ref-yields-empty-diff]`

## Runner `--files` flag

Which directly-changed test/eval files a runner receives via `--files` is declared by
**`files = true` projection tables**, not inferred from a file's extension. Each such table names a
CI output (`[unittest-files]` → `unittest-files`) and lists globs; a changed file is selected iff it
matches the table's globs.

CI projects the changed tag set into discovery dirs via `items <name>` (`--dirs`) and the file-set
globs into `--files`. The two compose by **union**: a test runs if it was discovered under `--dirs`
**or** listed in `--files`. That is what lets a changed test file still run when its subsystem tag
no longer matches it.

File-set tables are **projection-only**: they carry no tag-match `paths`, so they never participate
in the matched/expanded tag set, SHA buckets, touched flags, or `ignored`. Globs are scoped to the
code each runner can actually execute — a service owning a dedicated CI job (with its own
dependencies) is deliberately left out of the shared job's file-set, since routing its test files
there would run them in an environment lacking those deps.

- `--files GROUP=CSV` is repeatable; a pair without `=` is a usage error (exit 2). An empty csv is
  legal. `[runner-files-flag]`
- `files = true` tables select diff files by glob and stay out of tags/SHA/touched/ignored.
  `[file-set-projection]`

## `[runners]`

`explain` renders each items name's invocation from the `[runners]` table, keyed by **items name**
rather than by tag — a runner is a property of the item list, not of any one tag that contributes to
it. `files` names the file-set group feeding that runner's `--files` flag, made explicit because the
link is not derivable: items `unittest` pairs with the group `unittest-files`, and inferring one
from the other by stripping a suffix would be a guess.

An items name with no `[runners]` entry renders as a `# <name>: <csv>` comment line.

- Runner commands and their file-set groups come from config, not a hardcoded map. `[runners-section]`
