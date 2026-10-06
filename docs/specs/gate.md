# gate

> `tangier gate` records a local gate pass and lets CI reuse it. A gate is a set of commands from
> `pipeline.toml`. A pass leaves a record under a key made from content, uncommitted work
> included. CI computes the same key, finds the record, and does not run the commands again.

## Confidence

A verified gate carries the same confidence as a diff-based PR build, not more. Both rest on
`pipeline.toml` to say which content matters.

Three risks follow:

- **Scope under-coverage.** A gate whose scope omits a file it depends on gives a false pass. A
  diff-based build that omits the same file fails the same way. The backstop is a nightly full
  build that does not consult gate records.
- **Forged records.** Anyone with push access can write a gate ref. A record is the author's word,
  the same trust level as a commit status set by hand. A run's `runner` is self-reported too, so
  `--accept ci` narrows this risk by convention only: a dev machine can claim to be CI.
- **Environment drift.** The key covers the gate's `env` table and its scope. It does not cover the
  machine: tool versions, services, or environment variables outside `env`. A local pass and a CI
  pass can differ for those reasons.

## Config

```toml
[gate]
prune-after-days = 90

[gate.test-backend]
cmd   = ["bin/test --dirs {unittest-items} --files {unittest-files}", "bin/lint"]
env   = { TEST_DB_REQUIRED = "1" }
scope = ["core", "backend-gate-inputs"]

# A custom package: inputs that no SHA bucket covers.
[backend-gate-inputs]
paths = ["uv.lock", "pyproject.toml", "bin/test", "pipeline.toml"]
```

- `[gate] prune-after-days` is how old a record's newest run may be before `gate sync` prunes it.
  It is a whole number of days, 1 or more, and defaults to 90. It is the one scalar `[gate]`
  takes: any other is an unknown field. `[prune-after-days]`
- `[gate.<name>]` takes `cmd`, `env` and `scope` only. `cmd` and `scope` are required and
  non-empty, and each accepts a bare string for one entry. `env` is a table of strings. The name
  uses letters, digits, `-` and `_`, and does not start with `-`, because it becomes a ref
  component. A table without `cmd` is a [group](#groups). `[config-table]`
- Each command is split into arguments at parse time and runs without a shell. Shell operators and
  command substitution are rejected, as for `[deploy] after`. `[cmd-no-shell]`
- A placeholder is a whole argument: `{<name>-items}` for an items list, `{<group>}` for a
  `files = true` table. Any other use of braces is a config error. At run time the placeholder
  becomes the comma-joined list. An empty list gives an empty argument. `[placeholder-whole-token]`
- Each `scope` entry is a package: a SHA bucket, or a tag that lists `paths`. A bucket resolves to
  its member tags. A `files = true` table and a tag without `paths` are rejected. A package
  contributes its paths plus its transitive `depends`, minus `[sha] exclude` and per-tag `exclude`,
  exactly as a SHA bucket hashes. `[scope-packages]`
- `[sha] exclude` therefore removes files from the key too. With the default `**/README.md`, a
  gate whose commands read a README does not see a README change. Set `[sha] exclude` to fit.
- A custom package has no `sha` field, so it is not a SHA bucket. It stays out of `changemap sha
  --all`, `build-matrix` and the deploy versions. `[custom-package-not-a-bucket]`

- A tag in a gate scope counts as a CI signal for the `warn-tag-without-output` warning.
  `[scope-is-ci-signal]`
- A glob in a tag that feeds a gate scope must be a literal path or `dir/**`. Any other glob
  contributes nothing to the key, so it is a config error. A SHA bucket only warns: there the cost
  is a stale image tag, and here it is a false pass. `[unhashable-scope-glob-raises]`

## Groups

A gate is one record for all its commands, so a change that one command reads re-runs them all.
A group splits a gate into member gates. They share settings, and each keeps its own record.

```toml
[gate.lint]
scope = ["gate-inputs"]
env   = { RUFF_CACHE_DIR = ".ruff" }

[gate.lint.check]
cmd = "ruff check ."

[gate.lint.format]
cmd   = "ruff format --check ."
scope = ["docs"]
```

- A `[gate.<name>]` table with `cmd` is a gate. A table without `cmd` is a group. A group takes
  `env`, `scope` and member tables only, and needs at least one member. Each member is a gate
  table with `cmd`. Groups nest one level only. Flat gates and groups can sit side by side.
  `[group-table]`
- A member's full name is `<group>.<member>`, as `lint.check`. Each part follows the gate name
  rule. A member cannot be named `lock`, because git refuses a ref component that ends in `.lock`.
  Gates are kept in config order. `[group-table]`
- The group's `env` sits beneath each member's `env`, and the member's value wins. The group's
  `scope` comes before the member's, with duplicates removed. A member may leave out `scope` when
  the group sets one. The key, the need test and the run all read the merged gate. `[group-merge]`
- A group has no ordering field. When one command needs another to run first, such as a build
  before a type check, put both in the member that needs it. Each member then runs correctly on
  its own.
- Each member has its own key and record, under `refs/tangier/gates/<group>.<member>/<key>`. A
  change voids only the members whose scope it touches. `[group-records-per-member]`
  `[group-ref]`
- Output names map `.` to `-`: `lint.check` gives `lint-check-run`. Two gates, or a gate and a
  group, whose outputs would share a name are a config error. `[output-name-collision]`

## Selector

`gate run`, `gate key` and `gate verified` take selectors, not only gate names.

- A selector is a gate's full name, such as `lint.check`, or a group's name, such as `lint`. A
  group selects its members in config order. Any other selector is an error, exit 2. `gate run` runs the selected
  gates once each, in config order. `gate key` with a group prints `<name> <key>` for each member.
  `gate verified` with a group is verified only when every member has a record at `HEAD`.
  `[selector]`
- `gate key` with no selector prints `<name> <key>` for every gate, in config order, even when
  there is one. When any gate fails closed, it prints no keys. `[key-all]`

## Key

The key is the full SHA-1 of a JSON header and the scope's `git ls-tree -r <tree>` lines. The
header holds a key-schema version, the gate name, the raw commands and the `env` table, with
sorted keys. The key is a function of the config and the content only. The content is a commit's
tree, or the working tree for `gate run`, and for `gate key` and `gate verified` without `--head`.
A record therefore means "this gate passed for the scope as it stands in this tree", and any
record can serve as a [comparator](#comparator).

- Commit SHA, history, author and time are not inputs. A rebase or a re-cut that leaves the scope
  unchanged keeps the key. A change outside the scope keeps the key. A change inside the scope
  moves it. `[key-content-only]`
- The working tree keys as the tree `git add -A && git commit` would make: tracked changes and
  untracked files count, and ignored files do not. A clean tree keys as `HEAD`. Keying it does not
  touch the index. A submodule enters the tree as its checked-out commit, so uncommitted changes
  inside a submodule are an error. `[key-working-tree]`
- The raw commands are inputs, with each placeholder unresolved. The key does not read `--base`,
  so two runs at the same head with different bases share a key. A changed command moves the key.
  `[key-commands]`
- A gate with no placeholder takes nothing from the diff. With a record for the keyed tree, it needs no
  `--base` at all, so the gate works in a shallow checkout. `[key-no-placeholder-no-diff]`
- The `env` table is an input. `[key-env]`
- The key fails closed. Each of these is an error, because each would give one stable key that
  many trees share: `[key-fails-closed]`
  - a `--head` that names no commit;
  - a scope entry that matches no tracked file;
  - for a gate with a placeholder and no record for the keyed tree, a `--base` that names no commit or has
    no merge base with the head. The commands cannot be resolved without the diff. In a shallow
    clone, the error also names the fix: on a pull request, use `fetch-depth` of 2 or more with
    `--base HEAD^1`.

For a gate with a placeholder, a CI checkout must therefore reach the merge base, unless the
keyed tree already has a record. On a pull request, `--base HEAD^1` names the merge commit's first
parent, which is the merge base. A checkout with `fetch-depth` of 2 holds it.

CI computes the key on the PR merge commit. The key matches only when the merged in-scope content
equals what the local run tested. A moved `main` gives a miss, never a false hit.

## Comparator

A gate diffs from its **comparator**, not from `--base`. The comparator is the newest content, from
the working tree back through `HEAD` to the merge base with `--base`, that already has a record. A
run from it re-tests only what changed since the last pass. Soundness rests on induction: the comparator's own
record covered everything before it, and the root of the chain is the merge base, which `main`'s
full build covers.

- The working tree is checked first, which on a clean tree is `HEAD`. With a record there, the gate
  is verified. Otherwise, for a gate with placeholders, the newest commit with a record is the
  comparator. On a dirty tree that can be `HEAD` itself.
  Records are looked up locally first, then in one fetch of `origin`'s gate refs. `[comparator-newest-record]`
- The walk follows `HEAD`'s first-parent line down to the merge base. A record on a merged branch,
  reachable only through a second parent, is not used. A record on a commit that is not an
  ancestor is never reached. `[comparator-first-parent]`
- One merge is the exception: a pull request's merge commit. CI checks out a pull request as a
  merge commit whose first parent is the `main` tip, and with `--base HEAD^1` that parent is the
  merge base. So when `HEAD` is a merge commit whose first parent is the merge base, the walk
  goes on to the second parent, the PR head, and down its first-parent line. It stops where the
  branch left `main`. This step comes after the first-parent line and before the fall-back.
  `[comparator-pr-head]`
  - The diff stays two-dot, from the record to the keyed tree. When `main` moved, the diff holds
    `main`'s changes since the branch left it, as well as the branch's own. They were never tested
    together, so they run.
  - The rule reads the commit graph only, so a local `git merge --no-ff` onto `main` has the same
    shape and is walked the same way.
  - In a shallow clone, the walk stops at the shallow boundary. With no record above it, the
    gate diffs from the merge base. When `main` has moved further than the clone's depth, the
    walk can pass the fork point onto older `main` commits. Every commit it reaches is an ancestor
    of `HEAD`, so a longer or shorter walk costs extra runs, never a wrong skip.
  - `--debug` labels each step on this line `<sha> (PR head)`.
- With no record, the comparator is the merge base, as a run with no records would diff.
  `[comparator-falls-back-to-merge-base]`
- A record with no run that `--accept` takes is a miss, as if it had never been written. The walk
  goes on to an older commit, and falls back to the merge base. A record that cannot be read is a
  miss too, with a warning. `[comparator-ignores-rejected]`
- A gate with no placeholder checks the working tree only. An older record cannot narrow its
  commands, and if the scope has not changed since that record, the working tree has the same key.
  With no record there, it diffs from the merge base, and does not walk. `[comparator-no-placeholder]`

A rebase gives each commit a fresh key from its new content. When `main` did not touch the gate's
scope, the keys equal the old ones and the old records still match. When it did, they do not, and
the gate diffs from the new merge base: `main`'s changes and the branch's were never tested
together.

## Need

A gate is needed when the diff between its comparator and the keyed tree can change its result. `gate run` does
nothing for a gate the diff does not need, and `gate github-outputs` says so. The scope is the
whole rule: there is no separate config.

- A gate is needed only when a changed file touches its scope. A file touches the scope when it
  matches a scope tag or one of its transitive `depends` tags. A deleted file counts, and so does
  an uncommitted change or an untracked file. `[need-scope-touched]`
- Only key inputs count. A file that `[sha] exclude` or a tag's own `exclude` removes from the key
  does not touch the scope, because it cannot move the key. With the default `[sha] exclude`, a
  README-only change does not need the gate. `[need-key-inputs-only]`
- A gate with placeholders is not needed when every placeholder resolves to an empty list. One
  non-empty list makes it needed. `[need-empty-placeholders]`
- A `--base` with no merge base with `HEAD`, as in a shallow checkout, cannot be diffed. A gate
  with no placeholder and no record for the keyed tree is then needed, with a warning on stderr. Running
  the gate is the safe direction. In a shallow clone, the warning names the fix: on a pull request,
  use `fetch-depth` of 2 or more with `--base HEAD^1`. `[need-unreadable-base-runs]`

On a push to `main`, `origin/main` is `HEAD`, so the diff is empty and no gate is needed. A full
build uses `--full`.

## Store

A record is a JSON blob. The ref `refs/tangier/gates/<gate>/<key>` points at it. The record holds
a list of runs: each pass of the same content, on a dev machine or in CI, is one run.

- A record holds `format` (`2`), `gate`, `key` and `runs`. Each run holds these fields.
  `[record-contents]`

  | Field | Value |
  | --- | --- |
  | `head` | The commit the tested work sat on. |
  | `tree` | The tree that ran: `head`'s tree, or the working tree with uncommitted work. |
  | `dirty` | `true` when `tree` held uncommitted work. |
  | `base` | The comparator the run diffed from, or `null` when there was none. |
  | `user` | `git config user.email`, or `unknown`. |
  | `time` | ISO 8601, UTC. |
  | `duration` | Seconds the commands took, to 0.1 s. Runs that older tangier versions wrote have none. |
  | `tangier` | The tangier version. |
  | `commands` | The resolved commands. |
  | `runner` | Where the run happened. See below. |

  `base`, `commands`, `head` and `dirty` are for people. None is a key input.

- A pass on a dirty tree verifies the commit later made from exactly that work, with no new run.
  A commit of only part of the work keys differently, so the gate runs again.
  `[record-reused-after-commit]`

- A pass adds a run to the record at its key. Runs with the same `time`, `head` and `runner` are
  one run. A record keeps the newest 20 runs. `[record-runs]`
- A blob with no `runs` is a legacy record. It reads as one run whose runner is `local`: before
  runs were recorded, CI ran `--read-only` and wrote no record. A `format` above 2 is a newer
  record this tangier cannot read, so it is a miss, with a warning. `[record-legacy]`
- `runner.kind` is `ci` when the `CI` variable is `true` or `1`, and `local` otherwise. A local
  runner holds `host`. A CI runner holds `provider`: `github-actions` under GitHub Actions, and
  `unknown` elsewhere. Under GitHub Actions it also holds `event`, `ref`, `repository`, `workflow`,
  `job`, `run_id`, `run_attempt`, `runner_name` and the run's `url`. `[runner-detect]`

  A pull request and a merged branch are one store. `event` and `ref` tell their runs apart, and
  `--accept` can match on them.

- `gate run` writes a local ref. Once every gate in the invocation has run, an invocation that
  wrote a record publishes it: it pulls and merges `origin`'s gate records, as `gate sync` does but
  without pruning, and pushes only the refs it wrote, leased. A rejected push fetches and merges
  the written gates' refs alone and pushes again, up to 3 attempts. Other local records stay
  local. A failed publish is a warning that names `gate sync`, and the records stay local. The
  exit code is the gates'. `--read-only` and `--dry-run` write no record, so they never publish.
  `[run-publishes]`
- `gate sync` syncs every gate record with `origin`. It fetches `origin`'s gate refs, then pushes
  only the refs that differ, in one atomic push. With nothing to sync, it pushes nothing and exits
  0. `[sync]`
- A record that only `origin` holds is written to the local ref. `[sync-pulls]`
- When both hold a record at the same ref, `gate sync` merges their runs into the local record,
  and pushes it unless `origin`'s record already holds every run. A local record that cannot be
  read takes `origin`'s instead, with a warning. An `origin` record that cannot be read is
  overwritten, with a warning. `[sync-merges-runs]`
- Each pushed ref carries a lease on the SHA the fetch saw, or on the ref being absent. When
  another clone pushed after the fetch, the push is rejected, and `gate sync` fetches and merges
  again. So a race never overwrites another clone's runs. After 3 rejected attempts it exits 2,
  and this sync has written nothing to `origin`. `[sync-retries-on-race]`
- `gate sync` prunes. A record expires when its newest run's `time` is older than
  `prune-after-days`. A record with one recent run stays whole. An expired record is deleted
  locally and on `origin`, in the same atomic push. The delete is leased on the SHA the fetch saw,
  so a record that gained a fresh run since then is not deleted: the push is rejected, and the
  retry keeps it. A ref that is not a readable record is kept, with a warning. `[sync-prunes]`
- An expired record that `origin` lacks is deleted locally, not pushed. So a clone that synced
  before a prune never puts the pruned record back. `[sync-drops-expired-local]`
- A gate is verified when the local record holds an accepted run, or when `origin`'s record does.
  The local check runs first. The first lookup that reaches `origin` fetches its gate refs, in one
  call, into `refs/tangier/origin-gates/*`, and every later lookup reads that mirror.
  `[verified-local-then-origin]`
- An `origin` that cannot be read counts as not verified, with a warning on stderr. The gate then
  runs. `[origin-unreachable]`

A ref outside `refs/heads` and `refs/tags` triggers no workflow and no branch rule. Reading it needs
`contents: read` only.

## CLI

`--base` defaults to `origin/main`. For `gate key` and `gate verified`, no `--head` means the
working tree. For `gate github-outputs`, which CI runs on commits, `--head` defaults to `HEAD`.
`[cli-head-defaults]`

| Command | Behaviour |
| --- | --- |
| `gate list` | Print every gate and group. |
| `gate key [<selector>]` | Print the key. With no selector, print `<name> <key>` for every gate. |
| `gate run <selector> ...` | Run each gate on the working tree, or reuse a record. Takes `--all`, `--base`, `--read-only`, `--full`, `--fail-fast`, `--dry-run`, `--debug` and `--accept`. |
| `gate verified <selector>` | Print `verified` or `unverified`, and exit 0 or 1. The working tree is verified when its key has an accepted run. Takes `--accept`. |
| `gate sync` | Sync gate records with `origin`: pull, merge runs, push, and prune expired records. |
| `gate github-outputs` | Emit `<gate>-status`, `<gate>-run`, `<gate>-verified` and `<gate>-key` for every gate, and `<group>-status`, `<group>-run` and `<group>-verified` for every group. Takes `--accept` and `--full`. |

`gate run` has no `--head`. The commands test the checked-out tree, so the working tree is the only
content a record can describe. `gate key` and `gate verified` take no `--base`, because the key reads no diff.

- A gate name or selector that the config does not hold is an error, exit 2. `[unknown-gate]`
- `gate list` prints every gate in config order. A group's members are indented under a
  `<group> (group)` line. `[list]`
- `gate run` takes one or more [selectors](#selector), or `--all` for every configured gate in
  config order. No
  name and no `--all` is an error, exit 2. Each gate is planned and run in turn. A failing gate
  does not stop the rest, so one run reports every failure. The exit code is the first non-zero
  one. All gates share one read of `origin`. `[run-all]`
- `--fail-fast` stops at the first gate that fails or errors, and names the gates it did not run
  on stderr. The exit code is that gate's. Put cheap gates, such as lint and format, before slow
  ones in the config, so a cheap failure stops the run first. `[run-fail-fast]`
- `--dry-run` prints each gate's status, its comparator as a short SHA with how it was chosen
  (`record at abc1234, local` or `merge base with origin/main`), and the commands a run would
  execute. It runs nothing and writes nothing. When `--accept` rejected a record on the walk, the
  reason ends with how many, as `(2 record(s) ignored by --accept)`. `[run-dry-run]`
- `--debug` prints to stderr each commit the comparator walk checked, with its key and `miss`,
  `local` or `origin`. A dirty working tree shows as `working tree (<tree>)`. Under each commit it
  lists the runs it read, with where and when each ran, how long it took, and `accepted` or
  `ignored`. A run with no `duration` shows no time taken. It then prints the comparator, the
  changed files that touch the scope, and each placeholder's list. It combines with `--dry-run`.
  `[run-debug]`
- `--accept` says which runs count. Its value is a bare kind, `ci` or `local`, or comma-separated
  `field=value` pairs over `kind`, `provider`, `event`, `ref`, `workflow` and `job`. A run is
  accepted when every named field equals its runner's. The flag repeats, and a run is accepted
  when any value takes it. With no `--accept`, every run counts. Any other field or kind is an
  error, exit 2. The filter applies to every gate in the invocation. `[accept-filter]`

  ```sh
  tangier gate run e2e --accept ci                      # only CI runs count
  tangier gate run e2e --accept kind=ci,event=push      # only CI runs on merged branches
  tangier gate run e2e --accept ci --accept local       # any one may match
  ```

  `--accept` is not a key input: it changes which runs count, not what the content is. With
  `--accept ci` on a dev machine, a pass still records a `local` run, which does not satisfy the
  gate under the same filter.
- When the diff does not need the gate, `gate run` prints ``gate `<name>`: not-needed for this
  diff``, runs nothing, writes no record, and exits 0. The diff includes uncommitted work.
  `[run-not-needed]`
- When the gate is verified, `gate run` says where the record is, runs nothing, and exits 0.
  `[run-reuses-record]`
- Otherwise `gate run` runs each command with the gate's `env` added to the caller's environment.
  It stops at the first failure, exits with that command's code, and writes no record. It prints
  ``gate `<name>`: failed in 3.2s (exit <code>)``. `[run-stops-at-first-failure]`
- On success, `gate run` adds the run to the record, on a clean tree or a dirty one. It prints the
  time the commands took, the ref and the runner's kind:
  ``gate `<name>`: passed in 12.3s, recorded as <ref> (local)``. A run of a minute or more shows
  as `4m05s`. `[run-records-pass]`
- On a dirty tree, `gate run` prints a note to stderr for each gate before it plans. The note lists
  the uncommitted changes and untracked files that touch the gate's scope, or says that none do.
  It shows which local files the key holds, and helps explain a CI run whose key differs from
  `gate github-outputs`. A clean tree prints no note. `[run-dirty-notice]`
- On success, `gate run` writes no record and exits 1 when the working tree after the run differs
  from the tree it keyed, whether the commands or anything else changed it. The commands tested
  content that the key does not describe. A moved
  `HEAD` over the same content is fine, because the key reads content only. `[run-dirty-after]`
- `--read-only` writes no record, so the after-run check does not apply: a pass exits 0 whatever
  the commands left behind. It still reuses a record. `[run-read-only]`
- `--full` skips the need test, looks for no record and reads no diff. Each placeholder gets its
  complete list, as if every tag changed: every items path, and every tracked file that a
  file-set's globs match. It needs no merge base, so a shallow checkout works, and `--base` is
  ignored. It always runs the commands, and it writes the record by the usual rules. A complete
  file-set can repeat tests that the items already cover; `--dirs` and `--files` are a union, and
  a missed file would cost coverage. `[run-full]`
- The two flags combine:

  | Flags | Tests need | Reads a record | Writes a record |
  | --- | --- | --- | --- |
  | none | Yes | Yes | Yes |
  | `--read-only` | Yes | Yes | No |
  | `--full` | No | No | Yes |
  | `--read-only --full` | No | No | No |
- `gate github-outputs --full` gives every gate and every group the status `required`. It reads
  no record and no diff.
- `gate github-outputs` emits `<gate>-status=<status>`, `<gate>-run=true|false`,
  `<gate>-verified=true|false` and `<gate>-key=<key>` for each gate, in name order. `-run` is
  `true` when the status is `required`, for a plain `if:`. A gate the diff does not need still
  gets its `-key`. It echoes to stdout and appends to `$GITHUB_OUTPUT` when set. It reads `origin`
  once for all gates. A `.` in a member's name becomes `-`. `[github-outputs]`
- Each group gets `<group>-status`, `<group>-run` and `<group>-verified`, after the gates, in
  group name order. The status is `required` when any member is required, `verified` when every
  member is verified, and `not-needed` otherwise. `-run` is `true` when the status is `required`.
  A group has no `-key`. `[group-outputs]`
- The status is one word, checked in this order: `verified` when the keyed content has a record,
  `not-needed` when the diff from the comparator does not need the gate, and `required`
  otherwise. A CI job runs the gate when the status is `required`. `[status-values]`
