# 1. Map your app

Everything tangier does starts from one map: `pipeline.toml` splits the monorepo into **tags**,
each a named set of files. A diff selects the tags whose files it touches. Tests, gates and image
builds then follow from the selected tags.

Every key is in the [`pipeline.toml` reference](../../skills/tangier/reference/pipeline-toml.md).

## Tags

A tag is a table with `paths`:

```toml
[smartycore]
paths = "lib/smartycore/**"

[smartypants]
paths   = "service/smartypants/**"
depends = ["smartycore", "askastro-db"]
```

A changed file selects every tag whose `paths` match it. `exclude` subtracts globs from one tag
only, so another tag can still claim the file.

A tag usually maps one package: a service, a library, a frontend app. A tag can also map a
cross-cutting area, such as `.github/**` or `k8s/**`, so that a CI job can run only when that area
changes.

## Ignore by default

A file that matches no tag selects nothing. The map is an opt-in list of what matters to CI, not an
inventory of the repo. Docs, editor config and scratch files need no entry.

This is safe because the nightly build runs everything, with `--full`. A missing entry costs one
PR that tested too little, which the nightly then catches. `tangier changemap list-ignored` lists
the changed files that matched nothing, so you can check a diff.

## The `depends` graph

`depends` names the tags a tag relies on. When a tag changes, tangier also selects every tag that
depends on it, directly or through others. A change to `smartycore` above selects `smartypants`
too, and every other service that depends on it.

`tangier changemap list --graph` prints the graph. A dependency that names no tag, or a cycle, is a
config error.

## Patterns from a real monorepo

askastro maps a Python backend, seven images and a React frontend into about 80 tags. Three
patterns carry most of the weight.

**The library diamond.** Shared libraries depend on a core library, and every service depends on
the libraries it imports:

```toml
[smartycore]
paths = "lib/smartycore/**"

[askastro-db]
paths   = "lib/askastro-db/**"
depends = "smartycore"

[astronort-lector]
paths   = "service/lector/**"
depends = ["smartycore", "askastro-taskiq", "askastro-db"]
```

A change to `smartycore` reaches every service. A change to `askastro-db` reaches only the services
that use the database.

**The subsystem split.** The main service is cut into subsystems. Each subsystem with a UI has two
tags: `<name>` for the backend code, and `<name>-ui` for the UI code and its end-to-end tests.

```toml
[orgs]
depends        = "auth"
paths          = "service/smartypants/smartypants/orgs/**"
exclude        = ["**/*_test.py"]
unittest_items = "service/smartypants/smartypants/orgs"

[orgs-ui]
depends   = ["orgs", "e2e"]
paths     = ["frontend/src/org/**", "integration-tests/app/orgs/**"]
e2e_items = "integration-tests/app/orgs"
```

A backend change runs the matching e2e suite, because `orgs-ui` depends on `orgs`. A UI-only
change runs no backend unit tests. The `exclude` keeps a test-only change from running e2e: test
modules never change what the running app does.

**The `e2e` hub.** The Playwright config, the shared test harness and shared frontend code form one
`e2e` tag. Every `<name>-ui` tag depends on it, so a harness change runs every e2e suite.

## Check the map

```sh
tangier changemap list --graph                 # the depends graph
tangier changemap list-ignored --base HEAD~10  # changed files no tag claims
tangier changemap explain --base HEAD~10       # selected tags, and what CI would run
```

`explain` prints the tags a diff matched, the tags `depends` pulled in, and each test runner's
command line. It is the quickest way to see whether the map says what you mean.

Next: [selective tests](2-selective-tests.md).
