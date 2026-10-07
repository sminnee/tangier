# 3. Gates

A developer runs the tests before pushing. CI then runs the same tests again on the same code. A
**gate** removes the second run: it records a local pass, and CI skips a gate whose record matches
the content it is about to test.

Commands and flags are in the [CLI reference](../../skills/tangier/reference/cli.md#gate). The
design rules are in the [gate spec](../specs/gate.md).

## What a gate is

```toml
[gate.test-backend]
cmd   = "bin/test --dirs {unittest-items} --files {unittest-files}"
scope = ["smartypants", "test-backend-inputs"]
```

- **Commands.** `cmd` holds the commands, run in order without a shell. A `{...}` placeholder
  becomes an items list or a file-set, from the diff.
- **Key.** A pass is recorded under a key: a hash of the raw commands, the `env` table and the
  content of the `scope` packages. Commit SHAs and history are not inputs, so a rebase that leaves
  the scope alone keeps the record.
- **Record.** A record is a git ref, `refs/tangier/gates/<gate>/<key>`. `gate run` publishes
  the records it writes to `origin`, and CI reads them with `contents: read`.
- **Comparator.** A run diffs from the newest commit on the branch that has a record, or from the
  merge base. On a long branch, each run then re-tests only what changed since the last pass. In
  CI, a pull request's merge commit walks on down the PR head, so a record pushed from the branch
  counts.
- **Need.** A gate whose diff touches none of its scope, or whose placeholder lists are all empty,
  is `not-needed` and does no work.

So each gate is in one of three states: `verified`, `not-needed` or `required`. Only `required`
runs anything.

## The local loop

Run every gate:

```sh
tangier gate run --all     # each gate: verified, not-needed, or a run
```

Once its gates have run, `gate run` pulls `origin`'s records and pushes the ones it wrote, so CI
finds them. When `origin` cannot be reached or refuses the push, it warns, keeps the records local,
and still exits with the gates' result. `tangier gate sync` then sends them.

`gate sync` also pulls every record, merges runs and prunes. A record expires when its newest run
is older than `[gate] prune-after-days`, 90 by default. `gate sync` then deletes it locally and on
`origin`.

Gates the diff does not touch cost nothing, so a pre-push hook can run them all. askastro's
`bin/pre-push-gates`:

```sh
#!/bin/sh
set -e
for gate in lint-backend test-backend test-frontend test-frontend-units; do
  tangier gate run "$gate"
done
```

`gate run` keys the working tree, uncommitted work included. A pass before a commit is reused by
the commit made from exactly that work.

### Long runs

Outside CI, `gate run` runs the gates in a background job, so a shell's tool timeout cannot kill
them. A plain `gate run` returns once the job starts, with exit 3 and a hint that says how to pick
it up. `gate run --wait` waits for it, up to an hour, and exits with the gates' code. A second
`gate run --wait` queues behind a running job:

```sh
tangier gate run --all            # job 14: lint.check, test.py311 (a1b2c3d); exit 3, still running
tangier gate wait --job 14        # waits up to an hour; 0 passed, 1 failed, 3 still running
tangier gate run --all --wait     # starts a job and waits for it
tangier gate status               # recent jobs, each gate's key and state, and what is stale
tangier gate cancel               # stop the running job
```

When a job prints a load warning, the machine is busier than it has CPUs, so a timeout may come
from load and not the code.

CI and `--dry-run` run inline, with no job. One job runs at a time in a worktree. Jobs live in the
worktree's git directory, and old ones are pruned on each new job. See
[the spec](../specs/gate.md#jobs).

## Reporting failures

A failed run is recorded too, under `refs/tangier/failures/<gate>/<key>`, and published like a
pass. It holds the exit code, the time taken and where it ran. It never verifies a gate: `gate
verified` and CI read only `refs/tangier/gates`.

To record which tests failed, have the commands write a JUnit XML report, and name it in `junit`:

```toml
[gate.test-backend]
cmd   = "pytest --junitxml=build/junit-backend.xml -o junit_family=xunit1"
scope = ["smartypants", "test-backend-inputs"]
junit = "build/junit-backend.xml"
```

`gate run` deletes the file before the commands run, then reads it after. A failure keeps each
failing test's class, name, file, line and exception type, and drops the message, which the job
log holds. A pass and a failure both keep the counts. Put the report under a path git ignores, or
the report itself changes the working tree and the pass is not recorded. A missing or broken
report is a warning, and the run is recorded without it.

Most tools write JUnit:

| Tool | Flags | Terminal output |
| --- | --- | --- |
| pytest | `--junitxml=build/junit.xml -o junit_family=xunit1`; `xunit1` adds `file` and `line` | kept |
| vitest | `--reporter=default --reporter=junit --outputFile.junit=build/junit.xml` | kept |
| ruff | `check --output-format junit -o build/ruff.xml` | replaced by the file |
| eslint | `-f junit -o build/eslint.xml`, with the `eslint-formatter-junit` package on ESLint 9 | replaced by the file |
| unittest | `bin/unittest-junit build/junit.xml -s . -p 'test_*.py'`, copied from [tangier's repo](../../bin/unittest-junit); it wraps `unittest discover` | kept |

For ruff and eslint, a report costs you the terminal output, so only lint gates whose failures you
want to track need one. A lint report names each file and rule code, so `gate stats` can list the
files that keep failing.

## Gate statistics

`gate stats` reports on the runs in every record, local and `origin`'s:

```sh
tangier gate stats                   # the last 30 days, every gate
tangier gate stats test --since 7d   # one group, one week
tangier gate stats --ci --json       # CI runs only, as JSON
```

Each gate gets its runs, its pass rate, its median and 90th-percentile time for passes and
failures, and its median load per CPU. Its **flaky keys** are the keys with both a pass and a
failure: the same content gave both results. Then come the tests and files that failed most, for
gates that write a report.

The numbers have limits. A record keeps its newest 20 runs per key, and `gate sync` prunes it after
`prune-after-days`. A verified or not-needed gate ran nothing, so it is not counted. A
`--read-only` run writes no record, so a CI job that runs `--read-only` adds nothing to `--ci`.

## Scope and `*-inputs` packages

A scope entry is a package: a SHA bucket or a tag. The scope must cover every file the commands
read. A file left out gives a **false pass**: it changes, the key does not, and the old record still
matches.

Services cover their own code. The lockfile, the runner scripts, tool config and `pipeline.toml`
itself belong to no service. Put them in a custom package with no `sha`, named for the gate:

```toml
[test-backend-inputs]
paths = ["uv.lock", "pyproject.toml", "pipeline.toml", "bin/test", "docs/specs/**"]
```

## Groups

A gate is one record for all its commands. A change that one command reads re-runs them all. A
**group** splits a gate into members, each with its own record:

```toml
[gate.lint]
scope = ["smartypants", "lint-inputs"]

[gate.lint.pyright]
cmd = "bin/pyright-check --dirs {unittest-items}"

[gate.lint.vulture]
cmd   = "bin/vulture-check"
scope = ["vulture-allowlist"]
```

Members share the group's `env` and `scope`. `gate run lint` runs every member, and `gate run
lint.vulture` runs one. A group has no run order: put a prerequisite, such as a build, in the member
that needs it.

## CI-only gates

Some gates cannot run on a dev machine: they need CI services, secrets or hardware. CI runs them,
records the pass and publishes it. `--accept ci` makes only CI runs count, so a local run of the same
content does not verify the gate. See [GitHub Actions](5-github-actions.md#ci-only-gates).

## What voids a record

The key moves when its scope content, `cmd`, `env` or tangier's key version changes. The full list
is in the [gate checklist](../../skills/tangier/gate.md#what-voids-a-record). `gate run <gate>
--dry-run --debug` shows why a gate you expected to be verified is not.

## How much to trust a record

A verified gate is as trustworthy as a diff-based PR build: both trust the map. The [gate
spec](../specs/gate.md#confidence) lists the three gaps. The nightly covers all of them: it runs
every gate with `--full`, reading no record.

Next: [packages and builds](4-packages-and-builds.md).
