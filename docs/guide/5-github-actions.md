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
    tangier-version: ${{ steps.tangier.outputs.version }}
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
    - id: tangier
      run: |
        installed="$(uvx "$TANGIER" --version)"
        echo "version=${installed##* }" >> "$GITHUB_OUTPUT"
    - id: changes
      run: uvx "$TANGIER" changemap github-outputs
    - id: gates
      run: uvx "$TANGIER" gate github-outputs --summary
    - id: builds
      run: uvx "$TANGIER" changemap build-matrix
```

`--summary` adds a table of every gate's status, and who recorded each verified gate, to the
run's summary page.

The checkout needs the merge base; see
[checkout depth](../../skills/tangier/reference/github-actions.md#checkout-depth).

The plan also fixes the tangier version for the run. Every later job sets
`TANGIER: tangier==${{ needs.plan.outputs.tangier-version }}`, and passes the version to the shipped
actions as `tangier-version`. Image tags can hash differently from one release to the next, so a
build and an e2e job on different versions could disagree about a tag.

Every later job takes its path decisions from these outputs. The path rules stay in
`pipeline.toml`; a job that repeats them in YAML drifts from them.

## Gate jobs

A gate job runs when its gate is `required`:

```yaml
test-backend:
  needs: plan
  if: needs.plan.outputs.test-backend-run == 'true'
  env:
    TANGIER: tangier==${{ needs.plan.outputs.tangier-version }}
  steps:
    - uses: actions/checkout@v4
      with: { fetch-depth: 20 }
    - run: uvx "$TANGIER" gate run test-backend --read-only --base HEAD^1
```

The job takes no lists from the plan. `gate run` computes its comparator and lists itself.
`--base HEAD^1` names the PR merge commit's first parent, which is the merge base, so 20 commits of
history are enough. The comparator walks down the PR head to find the branch's newest record.
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

For an Amazon ECR registry, log in with an IAM role instead of a password. Set `[registry] url` to
the ECR host and namespace, such as `123456789012.dkr.ecr.ap-southeast-6.amazonaws.com/myapp`. Pass
`aws-role-arn` in place of the secret. The build assumes the role through GitHub OIDC, so the
calling job must grant `id-token: write`, as in the example below. The region comes from the host. Each image needs an ECR
repository, `<namespace>/<bucket>`, before its first push, unless a repository creation template
makes it on push. Leave tag immutability off, because `:latest` and `:buildcache` move.

To move a repo to another registry without a cutover day, do it in `pipeline.toml`, in three
steps. An older CLI rejects `mirrors`, so bump a pinned `tangier-version` first:

| Stage | `[registry]` |
| --- | --- |
| 1. Old registry | `url = "registry.sminn.ee/tangerine"` |
| 2. Both | `url = "registry.sminn.ee/tangerine"`, `mirrors = ["123456789012.dkr.ecr.ap-southeast-6.amazonaws.com/myapp"]` |
| 3. New registry | `url = "123456789012.dkr.ecr.ap-southeast-6.amazonaws.com/myapp"` |

The workflow does not change. Pass the password and the role from the start. The build
logs in only to the registries `pipeline.toml` names, and skips a credential none of them needs.

```yaml
build:
  needs: plan
  if: needs.plan.outputs.build-packages-empty == 'false'
  permissions:
    contents: read
    id-token: write
  uses: sminnee/tangier/.github/workflows/build.yaml@v0
  with:
    packages: ${{ needs.plan.outputs.build-packages }}
    aws-role-arn: arn:aws:iam::123456789012:role/ci/push-image
    mirrors: ${{ github.event_name != 'pull_request' }}
  secrets:
    registry-password: ${{ secrets.REGISTRY_PASSWORD }}
```

At stage 2, each `main` build pushes to both registries. A tag that one registry already has is
copied to the other, with no build, so one green `main` build fills the new registry with every
current image. A role that trusts only `main` cannot log in from a PR, so PR builds pass
`mirrors: false` and push to `url` alone. Nothing reads a mirror: compose files and deploys follow
`url`. So before stage 3, let PR builds assume the role, and let the deploy targets pull from the
new registry.

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
  run: uvx "$TANGIER" gate github-outputs --summary $FULL
```

Each gate job passes it on:

```yaml
- run: uvx "$TANGIER" gate run test-backend --read-only --base HEAD^1 $FULL
```

`--full` answers as if every tag changed
([what it fills](../../skills/tangier/reference/cli.md#shared-flags)), and every gate is
`required`. `gate run --full` reads no record, so the nightly is the backstop for every gap in the [gate trust
model](3-gates.md#how-much-to-trust-a-record).

Without `--full`, a run on `main` diffs `main` against itself. The diff is empty, so every list is
empty and a gate with placeholders tests nothing.

A green nightly triggers the prod deploy, from a second workflow on `workflow_run`. See the
[canonical workflow](../../skills/tangier/reference/github-actions.md#the-prod-deploy-after-a-green-nightly).

## Recording passes from CI

A `gate run` without `--read-only` records a pass and publishes it to `origin` in the same step.
No push step is needed.

- Give the job `permissions: contents: write`, and keep the credentials `actions/checkout` leaves
  behind. The publish pushes with them. A `--read-only` job needs `contents: read` only.
- A read-only token still lets the run pass. A pull request from a fork has one. The publish then
  warns, the record stays in the runner, and the gate runs again next time. Never switch to
  `pull_request_target` to get a write token: it runs the fork's code with that token.
- Each job publishes the records it wrote. A later job has nothing to send, because the records stay
  in the runner that wrote them.
- Jobs that record the same content at the same time are safe. Each push is leased, and a rejected
  one merges `origin`'s runs and tries again.
- A push to `refs/tangier/` triggers no workflow.
- Records age out on the next `gate sync --prune`, wherever it runs. A scheduled job keeps them
  pruned. See the
  [canonical workflow](../../skills/tangier/reference/github-actions.md#syncing-records).

## CI-only gates

A gate that needs CI's services or secrets is CI-only. Its job runs without `--read-only`, accepts
only CI runs, and [publishes the record](#recording-passes-from-ci). The job is in the
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
      with: { fetch-depth: 20 }
    - run: uvx "$TANGIER" gate run test-backend --read-only --base HEAD^1 $FULL
```

Keep the job's name, so a required status check still reports. A verified gate's job is skipped,
or passes in seconds. The [CI checklist](../../skills/tangier/ci.md#adopting-tangier-in-an-existing-workflow)
lists the remaining steps.

Next: [other features](6-other-features.md).
