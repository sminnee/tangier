# CLI reference

Every command and flag. `tangier <command> --help` prints the same flags for the installed version.

```sh
tangier [--config PATH] <group> <command> [args]
```

`--config` defaults to `$TANGIER_CONFIG`, then `pipeline.toml`. A missing or malformed config
exits 2, except for `tailnet check`, which runs without one. Any other tangier error prints
`error: ...` and exits 2.

## Shared flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--base REF` | `origin/main` | The diff runs from the merge base of `REF` and `--head`. |
| `--head REF` | `HEAD` | The commit to read. For `gate key` and `gate verified`, no `--head` means the working tree. |
| `--no-expand` | off | Skip `depends` expansion. For debugging. |
| `--full` | off | Answer as if every tag changed. Reads no diff, so `--base` is ignored and a shallow checkout works. |

`--full` fills every items list, sets every `touched` tag to `true`, and fills each file-set with
every tracked file its globs match. It is the switch for a nightly or any "run everything" build.

An unreadable `--base` gives an empty diff for the `changemap` commands, not an error. A gate with a
placeholder and no record fails instead. See `gate run`.

## `changemap`

Which tags a diff touches, and what that means for CI.

| Command | Flags | Output |
| --- | --- | --- |
| `changemap list` | `--graph` | Each tag and its globs. `--graph` prints each tag's `depends` instead. |
| `changemap list-ignored` | `--base`, `--head` | Changed files that match no tag. |
| `changemap explain` | `--base`, `--head`, `--no-expand`, `--full`, `--files GROUP=CSV` | Markdown: modified tags with their files, dependent tags, and each runner's invocation. |
| `changemap items <name>` | `--base`, `--head`, `--no-expand`, `--full` | One path per line of the items list `<name>`. An unknown name prints nothing and exits 0. |
| `changemap github-outputs` | `--base`, `--head`, `--no-expand`, `--full` | The answer set as `name=value` lines. |
| `changemap build-matrix` | `--base`, `--head`, `--no-expand`, `--full` | The buckets to build, as a JSON array. |
| `changemap sha [<bucket>]` | `--all`, `--github-notice`, `--head` | One bucket's hash, or every bucket's. |

`explain --files GROUP=CSV` repeats. It passes a file-set's list into that runner's `--files`. A
pair with no `=` exits 2. With `--full`, each file-set's complete list is the default. An empty
items list renders `--dirs ""`, and an empty file-set adds no `--files`.

### `changemap github-outputs`

Echoes each line to stdout, and appends it to `$GITHUB_OUTPUT` when that is set. Names come from the
config:

| Output | Value | From |
| --- | --- | --- |
| `<bucket>-sha` | the bucket's 10-hex hash | each SHA bucket |
| `<name>-items` | comma-joined paths, or empty | each `<name>_items` |
| `<tag>-touched` | `true` or `false` | each tag with `touched = true` |
| `<file-set>` | comma-joined files, or empty | each `files = true` table |

### `changemap build-matrix`

| Output | Value |
| --- | --- |
| `build-packages` | JSON array of buckets that have an `[image.*]` table and whose hash moved, such as `["astrochat","smartypants"]`. |
| `build-packages-empty` | `true` when the array is empty. An empty `strategy.matrix` is an error in GitHub Actions, so guard on this. |

A bucket reached only through `depends` is included: its hash moved. With `--full`, every bucket
with an `[image.*]` table is included.

### `changemap sha`

| Form | Output |
| --- | --- |
| `sha <bucket>` | The hash. An unknown bucket exits 2. |
| `sha --all` | `<BUCKET>_VERSION=<sha>` per bucket, for `export $(tangier changemap sha --all)`. |
| `sha --all --github-notice` | A markdown table, skipping buckets that end `-base`. |

## `image`

Content-addressed image tags and builds. A tag is the bucket's hash.

| Command | Flags | Behaviour | Exit |
| --- | --- | --- | --- |
| `image tag <bucket>` | `--head` | Print the bucket's tag. | 0, or 2 for an unknown bucket |
| `image exists <bucket>` | `--tag`, `--head` | Print `exists` or `missing`. Needs `regctl`. | 0 exists, 1 missing, 2 no `regctl` |
| `image build <bucket>` | see below | Run `docker buildx build`. | the build's exit code |
| `image compose <template>` | `--head` | Print the template with each `[[bucket]]` replaced by `<registry>/<bucket>:<sha>`. | 0 |

`image build` flags:

| Flag | Meaning |
| --- | --- |
| `--push` | Push to the registry, use the registry cache, and also tag `:latest`. Skip the build when the tag is already published. |
| `--load` | Build into the local Docker daemon instead. Excludes `--push`. |
| `--force` | Build even when the tag is already published. |
| `--tag TAG` | Use this tag instead of the bucket's hash. |
| `--head REF` | Hash the bucket at this commit. |
| `--secret id=<id>` | An extra buildx secret id. Passed only when its upper-cased variable is set. Repeats. |
| `--print` | Print the command line and exit. Needs no registry. |

`image build` emits `tag=<sha>` and `built=true|false` to stdout and `$GITHUB_OUTPUT`, except with
`--print`. `built` is `false` when the build was skipped or failed.

A bucket with no `[image.<bucket>]` exits 2.

## `deploy`

```sh
tangier deploy <env> [--summary] [--head REF]
tangier deploy --render <env>
tangier deploy --versions <env>
tangier deploy --compare-env <env>
```

| Flag | Meaning |
| --- | --- |
| `<env>` | Render, migrate, apply, wait, and roll back on failure. |
| `--render ENV` | Print the manifests a deploy would apply. Read-only. |
| `--versions ENV` | Print the version variables. Read-only. |
| `--compare-env ENV` | Print a markdown table of computed tags against the live environment. Read-only. |
| `--summary` | Write that table to `$GITHUB_STEP_SUMMARY`, or stdout, before deploying. Not with the read-only flags. |
| `--head REF` | Compute versions at this commit. |

Exit codes: 0 rolled out, 1 migration or rollout failure, 2 unknown environment, bad config or no
`kubectl`. A fatal `[deploy] after` hook that fails also exits non-zero.

## `tailnet`

| Command | Behaviour |
| --- | --- |
| `tailnet check [<env>]` | Check, in order: `tailscale` installed and up, `kubectl` installed, a context selected, the context is the operator's, the API server answers, and with `<env>`, this node carries `[tailnet.<env>] tag`. Stops at the first failure with the command that fixes it, exit 2. |

## `gate`

A selector is a gate's full name, such as `lint.check`, or a group's name, which selects every
member. An unknown selector exits 2.

| Command | Flags | Behaviour | Exit |
| --- | --- | --- | --- |
| `gate run <selector>...` | `--all`, `--base`, `--read-only`, `--full`, `--fail-fast`, `--dry-run`, `--debug`, `--accept`, `--timeout` | Run each gate on the working tree, unless it is verified or not needed. Then publish the records it wrote: pull `origin`'s records and push only those, with 3 attempts against a racing `origin`. A failed publish warns and does not change the exit code. Outside CI and `--dry-run`, start a job and wait for it. | the first non-zero code, or 3 when the job is still running |
| `gate wait [<selector>...]` | `--job N[,N...]`, `--timeout SECONDS` (default 60, `none` for no limit) | Wait for the latest job, or the listed jobs, or only the selected gates. Print each result, and the log tail of a failure. | 0 passed, 1 failed, 2 no such job, 3 still running |
| `gate status` | `--job N[,N...]`, `--since` (default `8h`), `--json` | Print recent jobs, each gate's key and state, and which results are stale. The latest job always shows. | 0 |
| `gate cancel` | `--job N` | Stop the running job's process group. Unfinished gates become `cancelled`. | 0 |
| `gate list` | | Print every gate in config order, with a group's members indented under a `<group> (group)` line. | 0 |
| `gate key [<selector>]` | `--head` | Print the key. A group, or no selector, prints `<name> <key>` per gate, and nothing when any gate fails closed. | 0, or 2 when a scope entry matches no file |
| `gate verified <selector>` | `--head`, `--accept` | Print `verified` or `unverified`. A group needs every member. | 0 verified, 1 unverified |
| `gate sync` | | Sync records with `origin`: pull, merge runs, push, and delete records whose newest run is older than `[gate] prune-after-days`, locally and on `origin`. Makes 3 attempts against a racing `origin`. | 0, or 2 |
| `gate github-outputs` | `--base`, `--head`, `--accept`, `--full` | Each gate's and group's status. | 0 |

### `gate run` flags

| Flag | Effect |
| --- | --- |
| `--all` | Every gate, in config order. Not with a selector. |
| `--read-only` | Reuse a record, but write none. |
| `--full` | Skip the need test and every record. Fill each placeholder with its complete list. Needs no merge base. |
| `--fail-fast` | Stop at the first gate that fails or errors, and name the gates not run on stderr. Exit with that gate's code. In a job, the gates not run end `cancelled`. |
| `--timeout SECONDS` | How long to wait for the job: 60 by default, `0` to return at once, `none` for no limit. |
| `--dry-run` | Print each gate's status, comparator, reason and commands. Run and write nothing. |
| `--debug` | Print the comparator walk, each run read with how long it took and whether `--accept` took it, the changed files, and each list, to stderr. |
| `--accept RAN_ON` | Count only runs from this runner: `ci`, `local`, or `field=value,...` over `kind`, `provider`, `event`, `ref`, `workflow` and `job`. Repeats; any match counts. |

| Flags | Tests need | Reads a record | Writes a record |
| --- | --- | --- | --- |
| none | yes | yes | yes |
| `--read-only` | yes | yes | no |
| `--full` | no | no | yes |
| `--read-only --full` | no | no | no |

Each gate prints one of:

- ``gate `<name>`: verified (...), nothing to run`` — a record matches this content.
- ``gate `<name>`: not-needed for this diff (...)`` — no key input changed, or every list is empty.
- the commands' output, then ``passed in <time>, recorded as <ref> (local|ci)``, ``passed in <time>, no record written (--read-only)``, or ``failed in <time> (exit <code>)``.

`gate run` exits 1 without a record when the working tree changed during the run. A gate with a
placeholder exits 2 when `--base` has no merge base and no record covers the tree; `--full` avoids
this.

### `gate github-outputs`

| Output | Value |
| --- | --- |
| `<gate>-status` | `verified`, `not-needed` or `required` |
| `<gate>-run` | `true` when `required` |
| `<gate>-verified` | `true` when `verified` |
| `<gate>-key` | the gate's key |
| `<group>-status`, `-run`, `-verified` | `required` if any member is, else `verified` if every member is, else `not-needed` |

A `.` in a name becomes `-`: `lint.check` gives `lint-check-run`. `--full` makes every status
`required`.
