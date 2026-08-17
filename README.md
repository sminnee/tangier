# tangier

A CI/deploy pipeline toolkit for monorepos, driven by one `pipeline.toml`.

It answers three questions that repos usually answer with a pile of copy-pasted shell scripts:

| | |
|---|---|
| `tangier changemap` | Which parts of the repo does this diff touch? Drives selective test runs. |
| `tangier image` | What is this component's content hash, and does that image already exist? |
| `tangier deploy` | Render and apply the k8s manifests for those image tags. |

The organising idea is the **content-addressed tag**: a component's image is tagged with a hash of
its own source tree plus everything it depends on. Rebuild only what changed, skip what is already
published, and deploy exactly what was built.

## Install

```sh
uv tool install /path/to/tangier      # or: pip install /path/to/tangier
```

Requires Python 3.11+ and **has no dependencies** — deliberately. tangier runs on CI runners that
have no `setup-python` step and no package installer available, so it must work against the system
Python. CI asserts both that the dependency list is empty and that every module imports under
`python -I`.

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

## Development

```sh
bin/test          # stdlib unittest, no dependencies
ruff check .
```

`bin/parity-check <path-to-repo>` diffs `tangier changemap` against a repo's pre-extraction
`bin/changemap` across many refs, in throwaway worktrees, and is the gate for migrating a repo onto
tangier. It is deliberately not part of CI — it needs a checkout of the consuming repo.

See `docs/specs/changemap.md` for the resolution rules in detail.
