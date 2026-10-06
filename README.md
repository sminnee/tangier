# tangier

A CI/deploy pipeline toolkit for monorepos, driven by one `pipeline.toml`.

It answers the questions that repos usually answer with a pile of copy-pasted shell scripts:

| | |
|---|---|
| `tangier changemap` | Which parts of the repo does this diff touch? Drives selective test runs. |
| `tangier image` | What is this component's content hash, and does that image already exist? |
| `tangier deploy` | Render and apply the k8s manifests for those image tags. |
| `tangier tailnet` | Can this machine actually reach the cluster, and as what identity? |
| `tangier gate` | Has this content already passed its gate? Lets CI reuse a local pass. |

The organising idea is the **content-addressed tag**: a component's image is tagged with a hash of
its own source tree plus everything it depends on. Rebuild only what changed, skip what is already
published, and deploy exactly what was built.

## Install

```sh
uvx tangier@0.2.0 --help              # run a pinned release from PyPI
uv tool install tangier==0.2.0        # or put it on PATH
```

Without uv, `python3 -m tangier` runs it from a checkout.

Requires Python 3.11+ and **has no dependencies** — deliberately. tangier runs on CI runners that
have no `setup-python` step and no package installer available, so it must work against the system
Python. The `no-dependencies` gate asserts both that the dependency list is empty and that every
module imports with no site-packages, under `python3 -I -S`.

## Quick start

Copy `pipeline.example.toml` to `pipeline.toml` at your repo root and describe your repo as tags
over globs:

```toml
[core]
paths = "service/core/**"
depends = ["shared"]
unittest_items = "service/core"
sha = true
touched = true
```

Then:

```sh
tangier changemap explain                  # what would CI run for this diff?
tangier changemap sha --all                # every component's content hash
tangier changemap list-ignored             # changed files no tag claims
tangier image tag core                     # one component's hash
tangier image build core --push            # build, unless already published
tangier deploy --render uat                # the manifests a deploy would apply
tangier deploy uat                         # migrate, apply, wait, roll back on failure
tangier tailnet check uat                  # why can't I reach the cluster?
tangier gate run test-backend              # run the gate, and record a pass
tangier gate push                          # let CI find the record
```

`--config` defaults to `pipeline.toml`, overridable with `$TANGIER_CONFIG`.

## How resolution works

Resolution is **ignore-by-default**. The config is an opt-in list of paths that matter, not an
exhaustive map of the repo — a file matching no tag simply drops out. A missing entry costs a wasted
selective-CI run, never lost coverage, because full builds still run everything.

Tags form a dependency graph. `depends` is expanded as a *reverse*-transitive closure: when a shared
library changes, every tag that depends on it runs too. `tangier changemap list --graph` prints it.

A **SHA bucket** is a tag with `sha = true`. Its hash covers its own paths plus its transitive
dependencies' paths, so a change to a shared library moves every dependent image's tag. Docs are
excluded by default (`[sha] exclude`), so editing a README never triggers a rebuild.

## Deploy

`tangier deploy <env>` renders the overlay **once** and applies it in two passes: the migration Job
first, waited on, then everything. Both passes use the same rendered bytes, which is what makes the
Job re-apply a genuine no-op.

If the rollout does not complete — or pods start crash-looping past a threshold — it re-runs the
prior tag's migration and rolls each deployment back, one at a time, then exits 1. A migration
failure does *not* roll back: nothing has touched the Deployments yet, so the old pods are still
serving.

`--render` and `--versions` are read-only and are the cheapest way to see what a deploy will do.

`--summary` writes a build table comparing the computed tags against what is currently deployed. It
writes to `$GITHUB_STEP_SUMMARY` when that is set and to stdout otherwise, so one invocation works
both on a runner and on a laptop. The table is emitted before anything is applied, because the first
apply overwrites the tags it reads.

`[deploy] after` runs a command once a deploy has fully rolled out — never after a rollback:

```toml
[deploy]
after = "bin/sentry-release ${ENV}"
```

The command is split into arguments when the config is parsed, then each argument is substituted
against `ENV` and the version variables. No shell is involved, so shell operators (`&&`, `|`, `;`)
are rejected at parse time — put that logic in a script.

A failed hook does not fail the deploy. The string form above always means `fatal = false`; use the
table form to change that:

```toml
[deploy.after]
cmd = "bin/sentry-release ${ENV}"
fatal = true
```

## Gate

`tangier gate run <name>` runs a gate's commands and records a pass. CI finds the record and does
not run the commands again.

```toml
[gate.test-backend]
cmd = "bin/test --dirs {unittest-items} --files {unittest-files}"
env = { TEST_DB_REQUIRED = "1" }
scope = ["core", "backend-gate-inputs"]

[backend-gate-inputs]            # a custom package: inputs no SHA bucket covers
paths = ["uv.lock", "pyproject.toml", "bin/test", "pipeline.toml"]
```

The record sits under a **gate key**: a hash of the raw commands, the `env` table, and the
content of the `scope` packages. The key ignores commit SHA and history. A rebase or a re-cut that
leaves the scope unchanged keeps the record valid.

Each run diffs from the gate's **comparator**: the newest content, from the working tree back
through `HEAD` to the merge base with `--base`, that already has a record. On a branch built up over many commits, a gate
then re-tests only what changed since its last pass. With no record, it diffs from the merge base.

tangier decides whether that diff needs the gate. A gate is needed when a changed file is one of
its scope's key inputs and, for a gate with placeholders, at least one list is non-empty. A gate
the diff does not need does no work. So a pre-push hook runs every gate, then pushes the records:

```sh
tangier gate run --all || exit 1     # each gate: "not-needed", "verified", or a run
tangier gate push

tangier gate github-outputs          # in CI
# test-backend-status=required
# test-backend-run=true
# test-backend-verified=false
# test-backend-key=812f61d775681885026b167c33efd2155f04c861
```

`-status` is `verified`, `not-needed` or `required`. `-run` is `true` when it is `required`. A CI
job runs the gate only then, so the workflow holds no copy of the scope rules:

```yaml
test-backend:
  needs: gates
  if: needs.gates.outputs.test-backend-run == 'true'
```

A gate is one record for all its commands. When one command's inputs change, every command runs
again. To keep them apart, make a **group**: a `[gate.<name>]` table with no `cmd`, holding member
gates. The members share the group's `env` and `scope`, and each keeps its own record:

```toml
[gate.lint]
scope = ["backend"]

[gate.lint.pyright]
cmd = "pyright"

[gate.lint.vulture]
cmd   = "vulture"
scope = ["vulture-allowlist"]          # added to the group's scope
```

`gate run lint` runs every member. `gate github-outputs` adds
`lint-pyright-run` for the member and `lint-run` for the group, which is `true` when any member
needs a run. A group has no run order: put a prerequisite in the member that needs it.

`gate run --all --dry-run` shows each gate's status, the commit it diffs from, and the commands it
would run. Add `--debug` to see each commit the comparator walk checked.

`gate run` keys the working tree, uncommitted changes and untracked files included. A pass before
a commit is reused by the commit made from that work, so committing does not rerun the gate. On a
dirty tree, each gate first lists the uncommitted files that touch its scope. If the commands
change the working tree, `gate run` writes no record and exits 1. These flags change what it does:

| Flag | Effect |
| --- | --- |
| `--read-only` | Reuse a record, but write none. |
| `--force` | Run the commands from the merge base, even when the gate is not needed or a record exists. |
| `--dry-run` | Print each gate's plan. Run nothing and write nothing. |
| `--debug` | Print the comparator walk and the diff to stderr. |

Records are git refs under `refs/tangier/gates/`. A local record needs no network. Reading records
in CI needs `contents: read` only. `gate prune --older-than 30` deletes old records.

The key reads no diff, so `gate key` and `gate verified` need no base. Without `--head` they key
the working tree, as `gate run` does. A gate with a `{...}` placeholder and no record for the keyed
tree needs the diff, so its checkout must hold `origin/main` and the merge base. Without them, `gate run` fails instead of running with empty lists. A gate with no
placeholder and no merge base counts as needed and runs, with a warning. On a push to `main` the diff is empty and no gate is needed, so a
full build passes `--force`.

A verified gate carries the same confidence as a diff-based PR build, not more. Keep a nightly full
build that does not consult gate records. See `docs/specs/gate.md` for the risks and the rules.

### Adopt gate in CI

Move each command from the workflow into `pipeline.toml`, and call the gate from the job. Before:

```yaml
test:
  strategy: { matrix: { python-version: ["3.11", "3.12", "3.13"] } }
  steps:
    - uses: actions/checkout@v4
    - uses: actions/setup-python@v5
      with: { python-version: "${{ matrix.python-version }}" }
    - run: bin/test
```

After, with one member gate for each Python version:

```toml
[gate.test]
scope = ["gate-inputs"]

[gate.test.py311]
cmd = "uv run --no-project --python 3.11 python -m unittest discover -s tangier -p test_*.py -t ."

[gate.test.py312]
cmd = "uv run --no-project --python 3.12 python -m unittest discover -s tangier -p test_*.py -t ."
```

```yaml
test:
  steps:
    - uses: actions/checkout@v4       # shallow: this gate has no placeholder
    - uses: astral-sh/setup-uv@v6
      with: { enable-cache: false }   # a verified gate never calls uv
    - name: Run test.py311
      env: { EVENT: "${{ github.event_name }}" }
      run: |
        flags=(--read-only)
        if [ "$EVENT" != pull_request ]; then flags+=(--force); fi
        uvx tangier@<version> gate run test.py311 "${flags[@]}"
    - name: Run test.py312
      # ...the same, for test.py312
```

With several gates, or a gate with a `{...}` placeholder, one `gates` job decides which gates run.
Each gate job is then a bare `gate run`, which finds its own comparator and item lists:

```yaml
gates:
  outputs:
    test-backend-run: ${{ steps.gates.outputs.test-backend-run }}
  steps:
    - uses: actions/checkout@v4
      with: { fetch-depth: 0, filter: "blob:none" }
    - id: gates
      run: uvx tangier@<version> gate github-outputs

test-backend:
  needs: gates
  if: needs.gates.outputs.test-backend-run == 'true'
  steps:
    - uses: actions/checkout@v4
      with: { fetch-depth: 0, filter: "blob:none" }
    - run: uvx tangier@<version> gate run test-backend --read-only
```

A group is one job with one step for each member. The job runs when any member needs a run. Each
step then runs its member, or reuses its record in seconds, and reports on its own:

```yaml
gates:
  outputs:
    python-run: ${{ steps.gates.outputs.python-run }}
  # ...as above

python:
  needs: gates
  if: needs.gates.outputs.python-run == 'true'
  steps:
    - uses: actions/checkout@v4
      with: { fetch-depth: 0, filter: "blob:none" }
    - run: uvx tangier@<version> gate run python.pyright --read-only
    - run: uvx tangier@<version> gate run python.ruff --read-only
    - run: uvx tangier@<version> gate run python.unittest --read-only
```

Add `if: "!cancelled()"` to the later steps to see every member's result when one fails.

| Rule | Reason |
| --- | --- |
| The command moves from the workflow to `[gate.<name>]`. The step becomes `tangier gate run <name>`. | One definition serves the developer and CI. |
| Run tangier as `uvx tangier@<version>`, after `astral-sh/setup-uv`. | A pinned release from PyPI. An unpinned `git+https` install runs whatever `main` holds, in a job that may have write access. |
| The job and its check name stay. | A required status check still reports. A verified gate passes in seconds. |
| On a pull request, pass `--read-only`. | CI reuses a record and runs on a miss. CI does not push records, and a run can leave report files in the tree. |
| On a push to `main` and on a nightly run, pass `--read-only --force`. | This is the full build that does not consult records. |
| A gate with no placeholder needs the default shallow checkout only. | Its key covers the commands and the scope at `HEAD`. |
| A gate with a `{...}` placeholder needs the merge base. Use `fetch-depth: 0` with `filter: blob:none`. | The item lists come from the diff. `fetch-depth: 0` alone fetches every blob of every branch. The blobless filter fetches commits and trees only. |
| A gate job takes no item lists from the `gates` job. | `gate run` computes the comparator and the lists itself, from the same diff. |
| A matrix dimension becomes one member gate for each leg, in a group. | The key has no matrix dimension. A member per leg gives each leg its own record. |
| A long gate whose commands read different inputs becomes a group. | A change then voids only the members whose scope it touches. |
| Setup steps still run. To skip them, add `tangier gate verified <name>` as an early step and put `if:` on the setup steps. | `gate run` saves the command time only. |
| Leave the pull request job on the merge commit, which is the checkout default. | The key then covers the merged content. A moved `main` gives a miss, never a false hit. |

tangier's own `pipeline.toml` and `.github/workflows/ci.yaml` follow these rules. Its gates have no
placeholder, so it keeps one job per group, one step per member, and no `gates` job. tangier has no nightly run.

## Actions

tangier ships the CI scaffolding as well as the CLI, so a consumer's workflows state their packages
and environments and nothing else.

| Action | Purpose |
|---|---|
| `sminnee/tangier/.github/actions/build@v0` | Build and push one package, skipping when already published |
| `sminnee/tangier/.github/actions/tailnet@v0` | Connect to the tailnet and point kubectl at the operator |
| `sminnee/tangier/.github/actions/deploy@v0` | The tailnet connect, plus `tangier deploy <env> --summary` |
| `sminnee/tangier/.github/workflows/build.yaml@v0` | Reusable matrixed build over a JSON array of packages |

Feed the build matrix from the CLI, which also emits a boolean for the empty case — an empty
`strategy.matrix` is a hard error in GitHub Actions, not a skip:

```sh
tangier changemap build-matrix
# build-packages=["astrochat","smartypants"]
# build-packages-empty=false
```

Read `docs/actions/tailnet.md` before touching any workflow that deploys. The separation between
uat and prod rests entirely on the `environment:` line of the calling job, and that is no longer
visible from the call site.

`@v0` is a moving alias: moving it ships to every consumer at once. Pin an exact version
(`@v0.2.0`) for reproducibility. § Development describes how a release is cut.

## Development

```sh
bin/test                  # stdlib unittest, no dependencies; one Python, any tree
tangier gate run test     # the suite on 3.11, 3.12 and 3.13, one member each; needs uv
tangier gate run lint     # lint.check and lint.format
tangier gate run no-dependencies  # no declared dependency, and no third-party import
tangier gate push         # before the PR push, so CI reuses the passes
```

Releases are a maintainer step, not part of the everyday loop. `bin/release v0.2.0` tags a version
and moves the `@v0` alias that the Actions pin. It:

- refuses a dirty tree, a HEAD that is not `origin/main`, and a tag that differs from the
  `pyproject.toml` version;
- runs the tests;
- pushes nothing.

Pushing the version tag publishes it to PyPI through `.github/workflows/release.yaml`. The workflow
uses trusted publishing, so the repo holds no PyPI token. Register the publisher once on pypi.org:
project `tangier`, repository `sminnee/tangier`, workflow `release.yaml`, environment `pypi`.

`skills/tangier/` teaches an agent to use tangier in any repo. `SKILL.md` is the entry point, and
each command with agent rules has its own file, such as `gate.md`. Link the skill from the checkout
that tracks `main`, so it follows that checkout:

```sh
ln -s /path/to/tangier/skills/tangier ~/.claude/skills/tangier
```

`bin/parity-check <path-to-repo>` diffs `tangier changemap` against a repo's pre-extraction
`bin/changemap` across many refs, in throwaway worktrees, and is the gate for migrating a repo onto
tangier. It is deliberately not part of CI — it needs a checkout of the consuming repo.

See `docs/specs/changemap.md` for the resolution rules in detail.
