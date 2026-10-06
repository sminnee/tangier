# tangier gate

A gate is a set of commands in `pipeline.toml`. A pass writes a record keyed by content, and CI
skips a gate whose record matches. Flags and outputs are in [reference/cli.md](reference/cli.md#gate).

## Before a PR push

1. Run `tangier gate run --all`. It keys the working tree, uncommitted work included. Each gate
   prints `verified`, `not-needed`, or runs its commands. When the project has
   [CI-only gates](#ci-only-gates), name the other gates instead.
2. When a gate fails, fix the cause and run that gate again. A failing gate does not stop the
   others, so read every failure in the output.
3. Commit exactly the work that passed. The commit reuses the record. A commit of only part of the
   work keys differently and needs a new run.
4. Run `tangier gate push`. It syncs the records with `origin`, so it needs network access to
   `origin`.

Done when `gate run --all` exits 0 on the committed tree and `gate push` exits 0.

When the repo has a pre-push hook that runs the gates, such as `bin/pre-push-gates`, a plain
`git push` does steps 1 and 4.

## Selectors and groups

`tangier gate run <group>.<member>` runs one member of a group. `tangier gate run <group>` runs
every member. While you work, run the narrowest selector that covers what you changed. Its pass is
recorded, and the commit of that work reuses it.

## CI-only gates

A CI-only gate needs CI services, secrets or hardware. Its CI job runs `gate run <gate> --accept
ci`. The CI workflow or the project's instructions name these gates.

- Do not run a CI-only gate locally. A local pass records a `local` run, which does not count.
- To see whether CI has covered your content, run `tangier gate run <gate> --accept ci --dry-run`.

## What voids a record

The key moves, and the gate runs again, when any of these change:

- a file in the gate's `scope`, or in a tag those scope entries `depends` on;
- the gate's `cmd` or `env`, including a group's `env` or `scope`;
- tangier's key version, after a tangier upgrade.

A rebase onto a `main` that touched the scope voids the record too. Run the gate again after any
rebase.

A change to a file that the commands read but no scope entry covers leaves the key alone, so the
old record gives a false pass. Add that file to the gate's `*-inputs` package; see
[setup.md](setup.md#add-a-gate).

## When a gate runs that you expected to be verified

Run `tangier gate run <selector> --dry-run --debug`. It runs nothing. It prints each commit the
comparator walk checked, with its key and `miss`, `local` or `origin`, and each run it read,
marked `accepted` or `ignored`. It then prints the commit the diff starts from, the changed files
in scope, and each placeholder's list. A key that differs at a commit you already gated means one
of the inputs above changed.
