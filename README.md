# tangier

tangier makes monorepo CI smart: it maps your app into packages, works out which tests, gates and
builds a change actually needs, and skips the rest — including anything you already ran locally.

One `pipeline.toml` at the repo root holds the map. tangier reads it locally and in CI, so the
developer and the workflow agree on what a change touches.

## What it does

1. **Runs only the tests a change can affect.** Tags map paths to packages, `depends` carries a
   library change to everything that uses it, and items lists turn the selected tags into test
   runner arguments.
2. **Versions packages by the files that changed, not by commits.** An image tag is a hash of the
   package's files and its dependencies' files. A docs-only commit, a rebase or a revert moves no
   tag, and a published tag is never rebuilt.
3. **Skips in CI what already passed locally.** A gate records a local pass under a key made from
   content. CI finds the record and skips the job.
4. **Builds many packages from one repo, each with its own version.** Each SHA bucket has its own
   `<BUCKET>_VERSION`, and a bucket with an `[image.*]` table builds as an image. CI builds the
   images a change touched, as one matrix.

## An example

askastro, a Python and React monorepo with seven images, maps a service like this:

```toml
[smartycore]                       # a shared library
paths = "lib/smartycore/**"

[astronort-lector]                 # a service: one image, its own version
paths          = "service/lector/**"
depends        = ["smartycore", "askastro-db"]
sha            = true
unittest_items = "service/lector"

[image.astronort-lector]
dockerfile = "service/lector/Dockerfile"

[gate.test-backend]                # run locally before a push; CI reuses the pass
cmd   = "bin/test --dirs {unittest-items} --files {unittest-files}"
scope = ["smartypants", "astronort-lector", "test-backend-inputs"]
```

A change to `lib/smartycore` selects every service that depends on it, runs their unit tests, and
rebuilds their images. A change to `service/lector` runs only lector's tests and builds only
lector's image. A pre-push hook runs the gates, and `gate run` publishes each pass to `origin`:

```sh
tangier gate run --all --wait
```

One CI workflow handles every event: a pull request runs what its diff selects, a push to `main`
builds and deploys to uat, and a nightly runs everything with `--full`.

A `plan` job runs `changemap github-outputs`, `gate github-outputs` and `changemap build-matrix`.
Every other job's `if:` reads one of its outputs, so no path rule is copied into YAML.

## Install

```sh
uvx tangier --help              # run the latest release from PyPI
uv tool install tangier         # or put it on PATH
```

Without uv, `python3 -m tangier` runs it from a checkout.

tangier needs Python 3.11 or later and has no dependencies. It runs on CI runners against the
system Python, with no `setup-python` step and no package installer.

## Documentation

Read the guide in order to learn tangier:

1. [Map your app](docs/guide/1-map-your-app.md): tags, `depends`, ignore-by-default.
2. [Selective tests](docs/guide/2-selective-tests.md): items lists, runners, file-sets, touched
   flags.
3. [Gates](docs/guide/3-gates.md): record a local pass, skip it in CI, and report pass rates and
   failing tests with `gate stats`.
4. [Packages and builds](docs/guide/4-packages-and-builds.md): content-hash versions and images.
5. [GitHub Actions](docs/guide/5-github-actions.md): the plan job, gate jobs, builds and the
   nightly.
6. [Other features](docs/guide/6-other-features.md): deploy and tailnet.

Look things up in the reference:

- [`pipeline.toml`](skills/tangier/reference/pipeline-toml.md): every key.
- [CLI](skills/tangier/reference/cli.md): every command, flag, output name and exit code.
- [GitHub Actions](skills/tangier/reference/github-actions.md): the canonical workflow and the
  shipped actions.

Follow a checklist:

- [Set up or extend `pipeline.toml`](skills/tangier/setup.md).
- [Write or change a CI workflow](skills/tangier/ci.md).
- [Run gates before a PR push](skills/tangier/gate.md).

The reference and checklists are the agent skill in `skills/tangier/`. Link it into an agent's
skills directory, and the agent reads the same pages:

```sh
ln -s /path/to/tangier/skills/tangier ~/.claude/skills/tangier
```

The design rules live in `docs/specs/`: [changemap](docs/specs/changemap.md) and
[gate](docs/specs/gate.md).

## Other features

tangier also deploys with kustomize (`tangier deploy`) and checks the Tailscale path to the cluster
(`tangier tailnet check`). See [other features](docs/guide/6-other-features.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development loop and the release process.
