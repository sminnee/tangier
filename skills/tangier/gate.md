# tangier gate

A gate is a set of commands in `pipeline.toml`. A pass writes a record keyed by content, and CI
skips a gate whose record matches. Flags and outputs are in [reference/cli.md](reference/cli.md#gate).

## Before a PR push

1. Run `tangier gate run --all --fail-fast --wait` as a background shell command, so the tool's
   foreground time limit does not apply. Its exit code is the gates' result. It keys the working
   tree, uncommitted work included. Each gate prints `verified`, `not-needed`, or runs its
   commands. When the project has
   [CI-only gates](#ci-only-gates), name the other gates instead.
2. When a gate fails, fix the cause and run step 1 again. Gates that passed are verified and do
   not run again, unless the fix touched their scope.
3. Commit exactly the work that passed. The commit reuses the record. A commit of only part of the
   work keys differently and needs a new run.
4. `gate run` publishes its records to `origin`. When any `gate run` printed `warning: gate records
   stay local`, run `tangier gate sync`. It needs network access to `origin`.

Done when `gate run --all --wait` exits 0 on the committed tree, and every `gate run` that warned
`gate records stay local` has been followed by a `gate sync` that exited 0.

When the repo has a pre-push hook that runs the gates, such as `bin/pre-push-gates`, a plain
`git push` does steps 1 and 4.

## Long runs

Outside CI, `gate run` starts a background job. A plain `gate run` returns once the job starts.
`--wait` waits for it, up to an hour. A killed or timed-out waiter leaves the job running. Only a
Ctrl-C in `gate run` cancels it.

- Exit 3 means the job is still running. Run the printed `tangier gate wait --job <n>` command,
  which waits up to an hour. Do not write a loop around it.
- A second `gate run --wait` queues behind the running job, then starts its own. A second
  `gate run` without `--wait` starts nothing and exits 2, with the running job's wait hint.
- When a gate fails on a timeout, look for a `load average` warning or note first. Load above the
  CPU count means other work slowed the gate. Rerun when load is lower before you debug the code.
- Do not edit the worktree while a job runs. A gate whose tree changes under it writes no record.
- Run `tangier gate status` to see each recent job and its gates. A `stale` result no longer
  matches your working tree. Run the gate again.
- `tangier gate cancel` stops the job.

## Failure history

Every failed run is recorded and published, under `refs/tangier/failures`. A failure never
verifies a gate.

- Before you debug a test failure, run `tangier gate stats <gate>`. When the gate shows flaky
  keys and the failing test is in its top failing list, run the gate once more before you debug.
  A test that fails again on the same content is a real failure.

## Selectors and groups

`tangier gate run <group>.<member>` runs one member of a group. `tangier gate run <group>` runs
every member. While you work, run the narrowest selector that covers what you changed. Its pass is
recorded, and the commit of that work reuses it.
`tangier gate list` shows the configured gates and groups.
`gate run --all` runs gates in config order, so list cheap gates, such as lint and format,
before slow ones.

## CI-only gates

A CI-only gate needs CI services, secrets or hardware, so only CI runs it. Its CI job runs
`gate run <gate> --accept ci`, and only a CI run verifies it. CI publishes its own records. The CI
workflow or the project's instructions name these gates.

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
