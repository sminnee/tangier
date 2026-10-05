# tangier gate

A gate is a set of commands in `pipeline.toml`. A pass writes a record. CI finds the record and
skips the run. When a case below is not covered, read `docs/specs/gate.md` in the tangier repo.

## Before a PR push

1. Run `tangier gate run --all`. It keys the working tree, uncommitted work included. Each gate
   prints `verified`, `not-needed`, or runs its commands. Gates the diff does not touch cost
   nothing.
2. When a gate fails, fix the cause and run that gate again. A failing gate does not stop the
   others, so read every failure in the output.
3. Commit exactly the work that passed. The commit reuses the record, so the gate does not run
   again. A commit of only part of the work keys differently and needs a new run.
4. Run `tangier gate push`. It needs network access to `origin`.

Done when `gate run --all` exits 0 on the committed tree and `gate push` reports the records it
pushed.

## Selectors and groups

A `[gate.<group>]` table without `cmd` is a group of member gates, each with its own record.
`tangier gate run <group>.<member>` runs one member. `tangier gate run <group>` runs every
member. While you work, run the narrowest selector that covers what you changed. Its pass is
recorded, and the commit of that work reuses it.

## What voids a record

A record matches while the gate's key is unchanged. The key moves when any of these change:

- a file in the gate's `scope`, or in a tag those scope entries `depends` on;
- the gate's `cmd` or `env`, including a group's `env` or `scope`;
- tangier's `KEY_VERSION`, after a tangier upgrade.

A rebase onto a `main` that touched the scope voids the record too. Run the gate again after any
rebase.

## When a gate runs that you expected to be verified

Run `tangier gate run <selector> --dry-run --debug`. It runs nothing. It prints each commit the
comparator walk checked with its key and `miss`, `local` or `origin`, the commit the diff starts
from, the changed files in scope, and each placeholder's list. A key that differs at a commit you
already gated means one of the inputs above changed.
