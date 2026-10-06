# GitHub Actions reference

The canonical workflow, and the actions tangier ships. Copy the workflow and replace the example
gates, jobs and buckets with your own.

## The canonical workflow

One file handles three events:

| Event | What runs |
| --- | --- |
| `pull_request` | Only the gates, tests and builds the diff needs. A gate with a local record is skipped. |
| `push` to `main` | Every image whose tag is not yet published, then the uat deploy. |
| `schedule` | Everything, with `--full`, reading no record. A pass triggers the prod deploy. |
| `workflow_dispatch` | The same as `schedule`, without the prod deploy. |

A push to `main` runs no tests: the pull request ran them, and the nightly backstops them. A build
on a pull request also moves each built image's `:latest` tag, because `image build --push` tags
`:latest`.

It fits `pipeline.example.toml`: a gate `test-backend` with placeholders, a gate group `lint`, an
items list `e2e`, a tag `k8s` with `touched = true`, and some `[image.*]` tables.

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [main]
  schedule:
    - cron: "0 7 * * *"
  workflow_dispatch:

permissions:
  contents: read

env:
  # A pinned release from PyPI. Bump it deliberately.
  TANGIER: tangier@0.2.1

jobs:
  plan:
    runs-on: ubuntu-latest
    outputs:
      full: ${{ steps.scope.outputs.full }}
      e2e-items: ${{ steps.changes.outputs.e2e-items }}
      k8s-touched: ${{ steps.changes.outputs.k8s-touched }}
      test-backend-run: ${{ steps.gates.outputs.test-backend-run }}
      lint-run: ${{ steps.gates.outputs.lint-run }}
      build-packages: ${{ steps.builds.outputs.build-packages }}
      build-packages-empty: ${{ steps.builds.outputs.build-packages-empty }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          filter: blob:none
      - uses: astral-sh/setup-uv@v6
      - name: Choose the scope
        id: scope
        env:
          EVENT: ${{ github.event_name }}
        run: |
          # The nightly, or a manual run, runs everything. Other events run what the diff needs.
          if [ "$EVENT" = schedule ] || [ "$EVENT" = workflow_dispatch ]; then
            echo "full=--full" >> "$GITHUB_OUTPUT"
          else
            echo "full=" >> "$GITHUB_OUTPUT"
          fi
      - name: Changed tags
        id: changes
        env:
          FULL: ${{ steps.scope.outputs.full }}
        run: |
          # shellcheck disable=SC2086
          uvx "$TANGIER" changemap github-outputs $FULL
          # shellcheck disable=SC2086
          uvx "$TANGIER" changemap explain $FULL >> "$GITHUB_STEP_SUMMARY"
      - name: Gates
        id: gates
        env:
          FULL: ${{ steps.scope.outputs.full }}
        run: |
          # shellcheck disable=SC2086
          uvx "$TANGIER" gate github-outputs $FULL
      - name: Builds
        id: builds
        env:
          EVENT: ${{ github.event_name }}
        run: |
          # On main, offer every image. `image build --push` skips a tag that is already published.
          if [ "$EVENT" = pull_request ]; then
            uvx "$TANGIER" changemap build-matrix
          else
            uvx "$TANGIER" changemap build-matrix --full
          fi

  test-backend:
    needs: plan
    if: needs.plan.outputs.test-backend-run == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          filter: blob:none
      - uses: astral-sh/setup-uv@v6
      # ...setup the commands need...
      - name: Run test-backend
        env:
          FULL: ${{ needs.plan.outputs.full }}
        run: |
          # shellcheck disable=SC2086
          uvx "$TANGIER" gate run test-backend --read-only $FULL

  lint:
    # A group is one job, with one step per member.
    needs: plan
    if: needs.plan.outputs.lint-run == 'true'
    runs-on: ubuntu-latest
    env:
      FULL: ${{ needs.plan.outputs.full }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          filter: blob:none
      - uses: astral-sh/setup-uv@v6
      - name: Run lint.pyright
        run: |
          # shellcheck disable=SC2086
          uvx "$TANGIER" gate run lint.pyright --read-only $FULL
      - name: Run lint.ruff
        if: "!cancelled()"
        run: |
          # shellcheck disable=SC2086
          uvx "$TANGIER" gate run lint.ruff --read-only $FULL

  lint-k8s:
    # A touched-only job: no gate, just a tag.
    needs: plan
    if: needs.plan.outputs.k8s-touched == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: bin/validate-k8s

  build:
    needs: plan
    if: needs.plan.outputs.build-packages-empty == 'false'
    uses: sminnee/tangier/.github/workflows/build.yaml@v0
    with:
      packages: ${{ needs.plan.outputs.build-packages }}
    secrets:
      registry-password: ${{ secrets.REGISTRY_PASSWORD }}

  e2e:
    # A selective job that is not a gate: it reads an items list from the plan.
    needs: [plan, build]
    if: >-
      !cancelled() && !failure() &&
      needs.plan.outputs.e2e-items != ''
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - name: Start the stack at this commit's tags
        run: |
          uvx "$TANGIER" image compose docker-compose.tmpl.yml > docker-compose.yml
          docker compose up -d
      - name: Run e2e
        env:
          E2E_ITEMS: ${{ needs.plan.outputs.e2e-items }}
        run: bin/e2e-test --dirs "$E2E_ITEMS"

  deploy-uat:
    needs: build
    if: >-
      !cancelled() && !failure() &&
      github.event_name == 'push'
    runs-on: ubuntu-latest
    environment: uat
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - uses: sminnee/tangier/.github/actions/deploy@v0
        with:
          env: uat
          client-id: ${{ vars.TS_DEPLOY_CLIENT_ID }}
          audience: ${{ vars.TS_DEPLOY_AUDIENCE }}
```

On the nightly, `--full` makes every items list complete and every `-run` output `true`, so the
same `if:` conditions run everything. Each job's `if:` reads a plan output and never repeats the
need logic.

### The prod deploy after a green nightly

A second workflow listens for the nightly to finish:

```yaml
name: Prod deploy

on:
  workflow_run:
    workflows: [CI]
    types: [completed]
    branches: [main]
  workflow_dispatch:

jobs:
  deploy-prod:
    if: >-
      github.event_name == 'workflow_dispatch' ||
      (github.event.workflow_run.event == 'schedule' &&
       github.event.workflow_run.conclusion == 'success')
    runs-on: ubuntu-latest
    environment: prod
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.workflow_run.head_sha || github.sha }}
      - uses: sminnee/tangier/.github/actions/deploy@v0
        with:
          env: prod
          client-id: ${{ vars.TS_DEPLOY_CLIENT_ID }}
          audience: ${{ vars.TS_DEPLOY_AUDIENCE }}
```

### A CI-only gate

A gate that only CI can run writes its own record. It takes no `--read-only`, accepts only CI runs,
and needs `contents: write` to push the record:

```yaml
  e2e-gate:
    needs: plan
    runs-on: ubuntu-latest
    permissions:
      contents: write
    env:
      FULL: ${{ needs.plan.outputs.full }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          filter: blob:none
      - uses: astral-sh/setup-uv@v6
      - run: |
          # shellcheck disable=SC2086
          uvx "$TANGIER" gate run e2e --accept ci $FULL
          uvx "$TANGIER" gate push
```

Run it on every event, not behind a `-run` output. It needs the plan only for `full`. `gate github-outputs` applies one
`--accept` to every gate, so without `--accept ci` there it counts local runs of this gate.

### Pruning records

```yaml
name: Prune gate records
on:
  schedule:
    - cron: "0 15 1 * *"
permissions:
  contents: write
jobs:
  prune:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uvx tangier@0.2.1 gate prune --older-than 30
```

## Checkout depth

| Job | Checkout |
| --- | --- |
| Runs `changemap` or `gate` against a diff | `fetch-depth: 0` and `filter: blob:none`. The diff needs the merge base. The filter skips file contents of old commits. |
| Runs only gates with no placeholder | The default shallow checkout. |
| Runs with `--full` only | The default shallow checkout. `--full` reads no diff. |
| Builds or deploys | Any. The tag hashes `HEAD`'s tree. |

## Shipped actions

Pin `@v0`, a moving alias that every release updates, or an exact `@v0.2.1`.

### `sminnee/tangier/.github/workflows/build.yaml@v0`

A reusable workflow: one matrix leg per bucket, each calling the `build` action. Feed it
`changemap build-matrix`. It skips itself for `[]`, and the caller should also guard on
`build-packages-empty`.

| Input | Default | Meaning |
| --- | --- | --- |
| `packages` | required | JSON array of buckets, as a string. |
| `registry-username` | `registry` | Registry user. |
| `extra-secrets` | `""` | Space-separated buildx secret ids. |
| `regctl-version` | `v0.6.1` | `regctl` release. |
| `tangier-ref` | the workflow's ref | tangier ref to install. |
| `fail-fast` | `false` | Cancel other legs when one fails. |
| `runs-on` | `ubuntu-latest` | Runner label. |

| Secret | Meaning |
| --- | --- |
| `registry-password` | Required. Registry password. |
| `sentry-auth-token` | Passed to every leg as `SENTRY_AUTH_TOKEN`. Used only by buckets whose `[image.*] secrets` name it. |

### `sminnee/tangier/.github/actions/build@v0`

Builds and pushes one bucket's image, unless its tag is already published.

| Input | Default | Meaning |
| --- | --- | --- |
| `package` | required | The bucket. Needs an `[image.<bucket>]` table. |
| `registry-password` | required | Registry password. |
| `registry-username` | `registry` | Registry user. |
| `extra-secrets` | `""` | Space-separated buildx secret ids. Pass values through the step's `env:`. |
| `regctl-version` | `v0.6.1` | `regctl` release. |
| `tangier-ref` | the action's ref | Required when the action is called by local path. |

| Output | Meaning |
| --- | --- |
| `tag` | The bucket's tag, built or not. |
| `published` | `true` when this run built and pushed. `false` when the tag already existed. |

### `sminnee/tangier/.github/actions/tailnet@v0`

Connects the runner to the tailnet as one environment's deploy identity and points `kubectl` at
the operator. The calling job's `environment:` line is what separates uat from prod: each
environment holds its own `TS_DEPLOY_CLIENT_ID` and `TS_DEPLOY_AUDIENCE`, so never drop or share it.

| Input | Default | Meaning |
| --- | --- | --- |
| `client-id` | required | Pass `${{ vars.TS_DEPLOY_CLIENT_ID }}`. |
| `audience` | required | Pass `${{ vars.TS_DEPLOY_AUDIENCE }}`. |
| `env` | `""` | Reads `[tailnet.<env>] tag`. |
| `tag` | `""` | The tag itself, for a repo with no `pipeline.toml`. |
| `operator` | `[tailnet] operator` | Operator hostname. |
| `version` | `1.78.1` | Tailscale release. |

| Output | Meaning |
| --- | --- |
| `tag` | The tag this runner authenticated as. |

The job needs `permissions: id-token: write`.

### `sminnee/tangier/.github/actions/deploy@v0`

The `tailnet` action, then `tangier deploy <env> --summary`.

| Input | Default | Meaning |
| --- | --- | --- |
| `env` | required | Needs a `[deploy.<env>]` table. |
| `client-id` | required | As for `tailnet`. |
| `audience` | required | As for `tailnet`. |
| `tailscale-version` | `1.78.1` | Tailscale release. |
| `tangier-ref` | the action's ref | Required when the action is called by local path. |

