# Write or change a CI workflow

A tangier workflow has one `plan` job that asks tangier what the change needs. Every other job reads
the plan's outputs in its `if:`. Start from the canonical workflow in
[reference/github-actions.md](reference/github-actions.md). Output names are in
[reference/cli.md](reference/cli.md).

## Write a workflow

1. Copy the canonical workflow.
2. In the `plan` job, add an output for each value a later job reads: `<gate>-run`, `<group>-run`,
   `<tag>-touched` or `<name>-items`.
3. Add one job per gate, and one job per gate group with one step per member. Each job's `if:`
   reads `<gate>-run` or `<group>-run`. Each step runs `gate run <gate> --read-only $FULL`.
4. Add one job per touched-only check. Its `if:` reads `<tag>-touched`.
5. Feed `build.yaml` from `build-matrix`. Do not write one build job per image.
6. Keep `on: schedule` in the same file, so the nightly runs the same gates and commands.
7. Lint the workflow with `actionlint` when it is installed.

Done when each job's `if:` reads only plan outputs and every gate in `pipeline.toml` has a job.

## Rules

| Rule | Reason |
| --- | --- |
| The command lives in `[gate.<name>]`. The step is `tangier gate run <name>`. | One definition serves the developer and CI, and the nightly cannot drift. |
| A job's `if:` reads a plan output and adds no path or tag logic. | tangier already decided. A copy in YAML drifts. |
| Run tangier as `uvx tangier@<version>`, after `astral-sh/setup-uv`. | A pinned release. An unpinned `git+https` install runs whatever `main` holds, in a job that may have write access. |
| On a pull request, pass `--read-only`, except for a CI-only gate. | CI reuses a record and runs on a miss. A run can leave report files in the tree, and a job that writes no record needs no write token. |
| On the nightly, pass `--full` to `changemap github-outputs`, `gate github-outputs` and every `gate run`. | `--full` fills every list and reads no record. Without it, a run on `main` has an empty diff and tests nothing. |
| On a manual run, do what the nightly does. On a push to `main`, run `build-matrix --full`. | `image build --push` skips each tag already published, so only changed images build. |
| A job that diffs uses `fetch-depth: 0` and `filter: blob:none`. | The diff needs the merge base. See [checkout depth](reference/github-actions.md#checkout-depth). |
| A gate job takes no item lists from the plan job. | `gate run` computes the comparator and the lists itself. |
| A matrix dimension becomes one member gate per leg, in a group. | The key has no matrix dimension. |
| A gate whose commands read different inputs becomes a group. | A change then voids only the members whose scope it touches. |
| Add `if: "!cancelled()"` to each later member step in a group job. | Every member reports when one fails. |
| A job's name and check name stay when it moves to a gate. | A required status check still reports. A verified gate passes in seconds. |
| To skip setup steps for a verified gate, add `tangier gate verified <name>` as an early step and put `if:` on the setup steps. | `gate run` saves only the command time. |
| Leave the pull request checkout on the merge commit. | The key then covers the merged content. A moved `main` gives a miss, never a false hit. |

## CI-only gates

When a gate needs CI services, secrets or hardware:

1. Give its job `permissions: contents: write`.
2. Run `gate run <gate> --accept ci $FULL`, with no `--read-only`. It publishes its record, so
   add no push step.
3. Run the job on every event, not behind a `-run` output. It reads only `full` from the plan.
4. When `gate github-outputs` reports this gate, pass it `--accept ci` too. The flag applies to
   every gate in that call.

A pull request from a fork has no write token. Its CI-only gate runs and passes, and the publish
warns. Do not use `pull_request_target` to get a write token: it runs the fork's code with it.

## Adopting tangier in an existing workflow

1. Move each test command into a `[gate.*]` table. Follow [setup.md](setup.md#add-a-gate).
2. Replace the step's command with `gate run <name> --read-only $FULL`.
3. Replace each hand-written path filter or changed-files action with a plan output.
4. Replace a hand-written fan-out of build jobs with `build-matrix` and `build.yaml`.
5. Replace a separate nightly workflow's copied commands with `on: schedule` and `--full`.

Done when no workflow names a test command that `pipeline.toml` also holds.
