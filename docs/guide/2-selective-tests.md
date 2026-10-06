# 2. Selective tests

The map says which tags a diff selects. **Items lists** turn those tags into the arguments a test
runner takes, so CI runs only the tests the change can affect.

## Items lists

`<name>_items` on a tag adds paths to the list `<name>` when the tag is selected:

```toml
[auth]
paths          = "service/smartypants/smartypants/auth/**"
unittest_items = "service/smartypants/smartypants/auth"

[auth-ui]
depends   = ["auth", "e2e"]
paths     = "frontend/src/auth/**"
e2e_items = "integration-tests/app/auth"
```

A change under `auth/` selects `auth` and, through `depends`, `auth-ui`. The `unittest` list is then
`service/smartypants/smartypants/auth`, and the `e2e` list is `integration-tests/app/auth`. The
name before `_items` is yours to choose; each name is its own list.

`tangier changemap items unittest` prints one list. `changemap github-outputs` prints them all, as
`unittest-items=...` and `e2e-items=...`, for CI.

## Runners

`[runners]` tells `changemap explain` how each list is run:

```toml
[runners]
unittest = { cmd = "bin/test", files = "unittest-files" }
e2e      = { cmd = "bin/e2e-test" }
```

`explain` then prints `bin/test --dirs <unittest items>`. Your runner script takes `--dirs a,b,c`
and runs the tests under those directories. tangier never runs the runner itself; a
[gate](3-gates.md) or a CI step does.

## File-sets: changed tests that their tag no longer selects

A test file can sit outside the directory its tag lists. Or a tag can exclude test files, as the
subsystem tags in [the map](1-map-your-app.md) do. A changed test must still run. A **file-set**
selects changed files by its own globs:

```toml
[unittest-files]
files = true
paths = ["service/smartypants/**/*_test.py", "lib/**/*_test.py"]
```

The runner then takes `--files x,y` as well as `--dirs`, and runs the union. A file-set is never a
tag: it does not select tags, hash into images or count as ignored.

Keep a file-set's globs to the tests the runner's job can run. askastro leaves the typesetter's
tests out of `unittest-files`, because they need a browser that the backend job lacks.

## Touched flags

Some jobs are not test runs over a list. A Kubernetes manifest check runs whenever `k8s/**`
changes. `touched = true` gives the tag a boolean output:

```toml
[tags.k8s]
paths   = "k8s/**"
touched = true
```

`changemap github-outputs` then emits `k8s-touched=true` or `false`. `k8s` is a reserved name, so the
tag is declared under `[tags]`; its output keeps the plain name.

## In CI

```sh
tangier changemap github-outputs
# unittest-items=service/smartypants/smartypants/auth
# e2e-items=integration-tests/app/auth
# k8s-touched=false
# unittest-files=service/smartypants/smartypants/auth/session_test.py
```

A CI job reads these outputs in its `if:` and its arguments. [GitHub Actions](5-github-actions.md)
builds the whole workflow. For a test command you also run locally, a [gate](3-gates.md) reads the
same lists itself, and adds record reuse.

Next: [gates](3-gates.md).
