# 5. GitHub Actions

This chapter builds a workflow that runs only what a change needs. The finished file is the
[canonical workflow](../../skills/tangier/reference/github-actions.md#the-canonical-workflow). The
rules for changing it are in the [CI checklist](../../skills/tangier/ci.md).

## One workflow, three events

A pull request runs what its diff selects. A push to `main` builds unpublished images and deploys
to uat. The nightly runs everything with `--full`. The
[canonical workflow](../../skills/tangier/reference/github-actions.md#the-canonical-workflow) has
the table.

All three live in one file. The nightly then runs the same gates, the same commands and the same
jobs as a pull request. A separate nightly file copies the commands, and the copies drift.

## The plan job

One job asks tangier what the change needs, and exposes the answers as job outputs:

```yaml
plan:
  outputs:
    full: ${{ steps.scope.outputs.full }}
    e2e-items: ${{ steps.changes.outputs.e2e-items }}
    k8s-touched: ${{ steps.changes.outputs.k8s-touched }}
    test-backend-run: ${{ steps.gates.outputs.test-backend-run }}
    build-packages: ${{ steps.builds.outputs.build-packages }}
    build-packages-empty: ${{ steps.builds.outputs.build-packages-empty }}
  steps:
    - uses: actions/checkout@v4
      with: { fetch-depth: 0, filter: blob:none }
    - uses: astral-sh/setup-uv@v6
    - id: changes
      run: uvx "$TANGIER" changemap github-outputs
    - id: gates
      run: uvx "$TANGIER" gate github-outputs
    - id: builds
      run: uvx "$TANGIER" changemap build-matrix
```

The checkout needs the merge base; see
[checkout depth](../../skills/tangier/reference/github-actions.md#checkout-depth).

Every later job takes its path decisions from these outputs. The path rules stay in
`pipeline.toml`; a job that repeats them in YAML drifts from them.

## Gate jobs

A gate job runs when its gate is `required`:

```yaml
test-backend:
  needs: plan
  if: needs.plan.outputs.test-backend-run == 'true'
  steps:
    - uses: actions/checkout@v4
      with: { fetch-depth: 0, filter: blob:none }
    - run: uvx "$TANGIER" gate run test-backend --read-only
```

The job takes no lists from the plan. `gate run` computes its comparator and lists itself.
`--read-only` reuses a record but writes none: CI does not push records, and a run can leave report
files behind.

A gate group is one job, with one step per member. `<group>-run` is `true` when any member needs a
run. Each member step then runs, or reuses its own record in seconds.

## Touched-only and selective jobs

A job with no gate reads a touched flag or an items list:

```yaml
lint-k8s:
  needs: plan
  if: needs.plan.outputs.k8s-touched == 'true'

e2e:
  needs: [plan, build]
  if: "!cancelled() && !failure() && needs.plan.outputs.e2e-items != ''"
  steps:
    - env:
        E2E_ITEMS: ${{ needs.plan.outputs.e2e-items }}
      run: bin/e2e-test --dirs "$E2E_ITEMS"
```

## Builds

`build-matrix` feeds tangier's reusable build workflow, one matrix leg per image:

```yaml
build:
  needs: plan
  if: needs.plan.outputs.build-packages-empty == 'false'
  uses: sminnee/tangier/.github/workflows/build.yaml@v0
  with:
    packages: ${{ needs.plan.outputs.build-packages }}
  secrets:
    registry-password: ${{ secrets.REGISTRY_PASSWORD }}
```

On a pull request, the matrix holds the images the diff touched. On a push to `main`, the plan runs
`build-matrix --full`, which offers every image. Each leg skips a tag that is already published,
so only the changed images build. The uat deploy then follows the build.

## The nightly

The workflow also runs `on: schedule`. On that event the plan job sets `full=--full` and passes it
to every tangier call:

```yaml
- id: scope
  run: |
    if [ "$EVENT" = schedule ]; then echo "full=--full" >> "$GITHUB_OUTPUT"
    else echo "full=" >> "$GITHUB_OUTPUT"; fi
- id: changes
  run: uvx "$TANGIER" changemap github-outputs $FULL
- id: gates
  run: uvx "$TANGIER" gate github-outputs $FULL
```

Each gate job passes it on:

```yaml
- run: uvx "$TANGIER" gate run test-backend --read-only $FULL
```

`--full` answers as if every tag changed
([what it fills](../../skills/tangier/reference/cli.md#shared-flags)), and every gate is
`required`. `gate run --full` reads no record, so the nightly is the backstop for every gap in the [gate trust
model](3-gates.md#how-much-to-trust-a-record).

Without `--full`, a run on `main` diffs `main` against itself. The diff is empty, so every list is
empty and a gate with placeholders tests nothing.

A green nightly triggers the prod deploy, from a second workflow on `workflow_run`. See the
[canonical workflow](../../skills/tangier/reference/github-actions.md#the-prod-deploy-after-a-green-nightly).

## CI-only gates

A gate that needs CI's services or secrets is CI-only. Its job runs without `--read-only`, accepts
only CI runs, and pushes the record. The job is in the
[canonical workflow](../../skills/tangier/reference/github-actions.md#a-ci-only-gate). A developer
checks CI's coverage with `tangier gate run e2e --accept ci --dry-run`.

## Adopting tangier in existing CI

Before, a test job carries its command, and a separate path filter decides whether it runs:

```yaml
test-backend:
  if: needs.changes.outputs.backend == 'true'
  steps:
    - uses: actions/checkout@v4
    - run: bin/test service/smartypants service/lector
```

After, the command moves into `pipeline.toml`, and the job runs the gate:

```toml
[gate.test-backend]
cmd   = "bin/test --dirs {unittest-items} --files {unittest-files}"
scope = ["smartypants", "astronort-lector", "test-backend-inputs"]
```

```yaml
test-backend:
  needs: plan
  if: needs.plan.outputs.test-backend-run == 'true'
  steps:
    - uses: actions/checkout@v4
      with: { fetch-depth: 0, filter: blob:none }
    - run: uvx "$TANGIER" gate run test-backend --read-only $FULL
```

Keep the job's name, so a required status check still reports. A verified gate's job is skipped,
or passes in seconds. The [CI checklist](../../skills/tangier/ci.md#adopting-tangier-in-an-existing-workflow)
lists the remaining steps.

Next: [other features](6-other-features.md).
