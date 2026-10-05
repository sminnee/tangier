# gate

> `tangier gate` records a local gate pass and lets CI reuse it. A gate is a set of commands from
> `pipeline.toml`. A pass on a clean tree leaves a record under a key made from content. CI
> computes the same key, finds the record, and does not run the commands again.

## Confidence

A verified gate carries the same confidence as a diff-based PR build, not more. Both rest on
`pipeline.toml` to say which content matters.

Three risks follow:

- **Scope under-coverage.** A gate whose scope omits a file it depends on gives a false pass. A
  diff-based build that omits the same file fails the same way. The backstop is a nightly full
  build that does not consult gate records.
- **Forged records.** Anyone with push access can write a gate ref. A record is the author's word,
  the same trust level as a commit status set by hand.
- **Environment drift.** The key covers the gate's `env` table and its scope. It does not cover the
  machine: tool versions, services, or environment variables outside `env`. A local pass and a CI
  pass can differ for those reasons.

## Config

```toml
[gate.test-backend]
cmd   = ["bin/test --dirs {unittest-items} --files {unittest-files}", "bin/lint"]
env   = { TEST_DB_REQUIRED = "1" }
scope = ["core", "backend-gate-inputs"]

# A custom package: inputs that no SHA bucket covers.
[backend-gate-inputs]
paths = ["uv.lock", "pyproject.toml", "bin/test", "pipeline.toml"]
```

- `[gate.<name>]` takes `cmd`, `env` and `scope` only. `cmd` and `scope` are required and
  non-empty, and each accepts a bare string for one entry. `env` is a table of strings. The name
  uses letters, digits, `-` and `_`, and does not start with `-`, because it becomes a ref
  component. `[config-table]`
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

## Key

The key is the full SHA-1 of a JSON header and the scope's `git ls-tree -r <head>` lines. The
header holds a key-schema version, the gate name, the raw commands and the `env` table, with
sorted keys. The key is a function of the config and the commit only. A record therefore means
"this gate passed for the scope as it stands at this commit", and any record can serve as a
[comparator](#comparator).

- Commit SHA, history, author and time are not inputs. A rebase or a re-cut that leaves the scope
  unchanged keeps the key. A change outside the scope keeps the key. A change inside the scope
  moves it. `[key-content-only]`
- The raw commands are inputs, with each placeholder unresolved. The key does not read `--base`,
  so two runs at the same head with different bases share a key. A changed command moves the key.
  `[key-commands]`
- A gate with no placeholder takes nothing from the diff. With a record at `HEAD`, it needs no
  `--base` at all, so the gate works in a shallow checkout. `[key-no-placeholder-no-diff]`
- The `env` table is an input. `[key-env]`
- The key fails closed. Each of these is an error, because each would give one stable key that
  many trees share: `[key-fails-closed]`
  - a `--head` that names no commit;
  - a scope entry that matches no tracked file;
  - for a gate with a placeholder and no record at `HEAD`, a `--base` that names no commit or has
    no merge base with the head. The commands cannot be resolved without the diff.

For a gate with a placeholder, a CI checkout must therefore hold `origin/main` and enough history
to reach the merge base, unless `HEAD` already has a record.

CI computes the key on the PR merge commit. The key matches only when the merged in-scope content
equals what the local run tested. A moved `main` gives a miss, never a false hit.

## Comparator

A gate diffs from its **comparator**, not from `--base`. The comparator is the newest commit, from
`HEAD` back to the merge base with `--base`, whose content already has a record. A run from it
re-tests only what changed since the last pass. Soundness rests on induction: the comparator's own
record covered everything before it, and the root of the chain is the merge base, which `main`'s
full build covers.

- `HEAD` is checked first. With a record there, the comparator is `HEAD` and the gate is verified.
  Otherwise, for a gate with placeholders, the newest commit with a record is the comparator.
  Records are looked up locally first, then in one `ls-remote` of `origin`. `[comparator-newest-record]`
- The walk follows `HEAD`'s first-parent line down to the merge base. A record on a merged branch,
  reachable only through a second parent, is not used. A record on a commit that is not an
  ancestor is never reached. `[comparator-first-parent]`
- With no record, the comparator is the merge base, as a run with no records would diff.
  `[comparator-falls-back-to-merge-base]`
- A gate with no placeholder checks `HEAD` only. An older record cannot narrow its commands, and
  if the scope has not changed since that record, `HEAD` has the same key. With no record at
  `HEAD`, it diffs from the merge base, and does not walk. `[comparator-no-placeholder]`

A rebase gives each commit a fresh key from its new content. When `main` did not touch the gate's
scope, the keys equal the old ones and the old records still match. When it did, they do not, and
the gate diffs from the new merge base: `main`'s changes and the branch's were never tested
together.

## Need

A gate is needed when the diff between its comparator and `HEAD` can change its result. `gate run` does
nothing for a gate the diff does not need, and `gate github-outputs` says so. The scope is the
whole rule: there is no separate config.

- A gate is needed only when a changed file touches its scope. A file touches the scope when it
  matches a scope tag or one of its transitive `depends` tags. A deleted file counts.
  `[need-scope-touched]`
- Only key inputs count. A file that `[sha] exclude` or a tag's own `exclude` removes from the key
  does not touch the scope, because it cannot move the key. With the default `[sha] exclude`, a
  README-only change does not need the gate. `[need-key-inputs-only]`
- A gate with placeholders is not needed when every placeholder resolves to an empty list. One
  non-empty list makes it needed. `[need-empty-placeholders]`
- A `--base` with no merge base with `HEAD`, as in a shallow checkout, cannot be diffed. A gate
  with no placeholder and no record at `HEAD` is then needed, with a warning on stderr. Running
  the gate is the safe direction. `[need-unreadable-base-runs]`

On a push to `main`, `origin/main` is `HEAD`, so the diff is empty and no gate is needed. A full
build uses `--force`.

## Store

A record is a JSON blob. The ref `refs/tangier/gates/<gate>/<key>` points at it.

- A record holds these fields. `[record-contents]`

  | Field | Value |
  | --- | --- |
  | `gate` | The gate name. |
  | `key` | The key. |
  | `head` | The commit that ran. |
  | `base` | The comparator the run diffed from, or `null` when there was none. |
  | `user` | `git config user.email`, or `unknown`. |
  | `time` | ISO 8601, UTC. |
  | `tangier` | The tangier version. |
  | `commands` | The resolved commands. |

  `base` and `commands` are for people. Neither is a key input.

- `gate run` writes a local ref. It needs no network.
- `gate push` pushes every local gate ref to `origin` with a forced refspec. Two people can record
  the same key with different blobs. The last writer wins, and both blobs mean the same pass. With
  no local record, `gate push` does nothing and exits 0. `[push]`
- A gate is verified when the local ref exists, or when `git ls-remote origin` returns the ref. The
  local check runs first. `[verified-local-then-origin]`
- An `origin` that cannot be read counts as not verified, with a warning on stderr. The gate then
  runs. `[origin-unreachable]`

A ref outside `refs/heads` and `refs/tags` triggers no workflow and no branch rule. Reading it needs
`contents: read` only.

## CLI

`--base` defaults to `origin/main` and `--head` to `HEAD`, as for `changemap`.

| Command | Behaviour |
| --- | --- |
| `gate key <name>` | Print the key. |
| `gate run <name> ...` | Run each gate at `HEAD`, or reuse a record. Takes `--all`, `--base`, `--read-only`, `--force`, `--dry-run` and `--debug`. |
| `gate verified <name>` | Print `verified` or `unverified`, and exit 0 or 1. `HEAD` is verified when its key has a record. |
| `gate push` | Push local gate records to `origin`. |
| `gate github-outputs` | Emit `<gate>-status`, `<gate>-run`, `<gate>-verified` and `<gate>-key` for every gate. |
| `gate prune --older-than <days>` | Delete old gate records, on `origin` and in this clone. |

`gate run` has no `--head`. The commands test the checked-out tree, so `HEAD` is the only commit a
record can describe. `gate key` and `gate verified` take no `--base`, because the key reads no diff.

- A gate name that the config does not hold is an error, exit 2. `[unknown-gate]`
- `gate run` takes one or more gate names, or `--all` for every configured gate in name order. No
  name and no `--all` is an error, exit 2. Each gate is planned and run in turn. A failing gate
  does not stop the rest, so one run reports every failure. The exit code is the first non-zero
  one. All gates share one read of `origin`. `[run-all]`
- `--dry-run` prints each gate's status, its comparator as a short SHA with how it was chosen
  (`record at abc1234, local` or `merge base with origin/main`), and the commands a run would
  execute. It runs nothing, writes nothing, and does not refuse a dirty tree. `[run-dry-run]`
- `--debug` prints to stderr each commit the comparator walk checked, with its key and `miss`,
  `local` or `origin`. It then prints the comparator, the changed files that touch the scope, and
  each placeholder's list. It combines with `--dry-run`. `[run-debug]`
- Without `--read-only`, `gate run` refuses a dirty tree with exit 2 and runs nothing. A tracked change and an untracked
  file both make the tree dirty, whatever `status.showUntrackedFiles` says. Ignored files do not.
  `[run-refuses-dirty-tree]`
- When the diff does not need the gate, `gate run` prints ``gate `<name>`: not-needed for this
  diff``, runs nothing, writes no record, and exits 0. The dirty-tree check comes first, because the
  need test reads commits only. `[run-not-needed]`
- When the gate is verified, `gate run` says where the record is, runs nothing, and exits 0.
  `[run-reuses-record]`
- Otherwise `gate run` runs each command with the gate's `env` added to the caller's environment.
  It stops at the first failure, exits with that command's code, and writes no record.
  `[run-stops-at-first-failure]`
- On success with a clean tree, `gate run` writes the record. `[run-records-pass]`
- On success, `gate run` writes no record and exits 1 when the commands left the tree dirty or
  moved `HEAD`. The commands tested content that the keyed commit does not hold.
  `[run-dirty-after]`
- `--read-only` writes no record. The clean-tree and `HEAD` rules protect the record, so they do not
  apply. A dirty tree runs, with a note on stderr. A pass exits 0 whatever the commands left
  behind. A clean tree still reuses a record. A dirty tree reads no record, because the key
  describes `HEAD`. The item and file lists still come from the commits between `--base` and
  `HEAD`, not from uncommitted changes. `[run-read-only]`
- `--force` skips the need test and does not look for a record. It resolves the placeholders
  against the merge base with `--base`, so it is a full run. It always runs the commands, and it
  writes the record by the usual rules. `[run-force]`
- The two flags combine:

  | Flags | Dirty tree at start | Tests need | Reads a record | Writes a record |
  | --- | --- | --- | --- | --- |
  | none | Exit 2 | Yes | Yes | Yes |
  | `--read-only` | Runs | Yes | On a clean tree only | No |
  | `--force` | Exit 2 | No | No | Yes |
  | `--read-only --force` | Runs | No | No | No |
- `gate github-outputs` emits `<gate>-status=<status>`, `<gate>-run=true|false`,
  `<gate>-verified=true|false` and `<gate>-key=<key>` for each gate, in name order. `-run` is
  `true` when the status is `required`, for a plain `if:`. A gate the diff does not need still
  gets its `-key`. It echoes to stdout and appends to `$GITHUB_OUTPUT` when set. It reads `origin`
  once for all gates. `[github-outputs]`
- The status is one word, checked in this order: `verified` when the comparator is `HEAD`,
  `not-needed` when the diff from the comparator does not need the gate, and `required`
  otherwise. A CI job runs the gate when the status is `required`. `[status-values]`
- `gate prune` fetches the gate refs on `origin` into `refs/tangier/origin-gates/*`, reads the
  `time` of each record, and deletes the refs older than the limit on `origin`. It deletes this
  clone's local records by the same rule, because `gate push` would put them back. The limit is
  1 day or more. `[prune-by-record-time]`
- Another clone that holds an old local record puts the ref back on its next `gate push`. The next
  prune deletes it again.
- `gate prune` skips a ref that is not a readable record, with a warning. `[prune-skips-unreadable]`
