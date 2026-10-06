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
  merge base. On a long branch, each run then re-tests only what changed since the last pass.
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
