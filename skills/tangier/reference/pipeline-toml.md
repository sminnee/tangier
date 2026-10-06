# `pipeline.toml` reference

Every key tangier reads. The file sits at the repo root. `--config` or `$TANGIER_CONFIG` points
elsewhere. An unknown key on any table is a config error, exit 2.

## Top-level names

Each top-level table is a tag, except these reserved names:

| Name | Configures |
| --- | --- |
| `tags` | Tags whose names collide with a reserved name. |
| `sha` | How SHA buckets hash. |
| `registry` | Where images are pushed. |
| `runners` | How `changemap explain` renders each items list. |
| `image` | How each bucket's image builds. |
| `k8s` | Which deployments each bucket's image backs. |
| `deploy` | Deploy environments, rollout tuning and the after-deploy hook. |
| `tailnet` | How CI reaches the cluster. |
| `gate` | Gates and gate groups. |

A tag that needs a reserved name is written `[tags.<name>]`, such as `[tags.k8s]`. Its outputs keep
the plain name, so `[tags.k8s]` with `touched = true` emits `k8s-touched`. A name declared both bare
and under `[tags]` is an error. `depends` works across the two forms.

## Tags

```toml
[smartypants]
paths   = "service/smartypants/**"
depends = ["smartycore", "askastro-db"]
sha     = true
touched = true
```

| Key | Type | Meaning |
| --- | --- | --- |
| `paths` | string or list | Globs of the files that belong to the tag. A tag with only `depends` is an aggregator. |
| `exclude` | string or list | Globs subtracted from this tag's `paths`. Another tag can still claim the file. Also filters the tag's SHA bucket. |
| `depends` | string or list | Tags this tag depends on. A change to a dependency also selects this tag. |
| `<name>_items` | string or list | Paths added to the items list `<name>` when this tag is selected. |
| `sha` | `true` or string | `true` makes the tag a SHA bucket of its own name. A string adds the tag to that bucket. |
| `touched` | bool | Emit `<tag>-touched=true\|false` from `changemap github-outputs`. |
| `files` | `true` | Makes the table a file-set instead of a tag. See below. |

Rules:

- A glob is anchored at both ends. `**` matches any characters, `/` included. `*` and `?` stop at
  `/`. `**/README.md` matches a root `README.md` too.
- A tag that feeds a SHA bucket or a gate scope may use only literal paths and `dir/**` globs.
  `git ls-tree` cannot walk any other glob. A SHA bucket warns, and a gate scope is an error.
- A `depends` entry that names no tag is an error. So is a cycle.
- A tag with no `paths` and no `depends` warns: it can never be selected.
- A tag with no `sha`, `touched`, `*_items`, dependent or gate scope warns: it has no CI output.

## File-sets

```toml
[unittest-files]
files = true
paths = ["service/smartypants/**/*_test.py", "lib/**/*_test.py"]
```

A table with `files = true` selects changed files by its own globs, for a runner's `--files`
argument. It takes `paths` only; any other key is an error. It is never matched, expanded, hashed, touched or ignored. The
table name is its output name and its placeholder name: `unittest-files=` and `{unittest-files}`.

## `[sha]`

| Key | Default | Meaning |
| --- | --- | --- |
| `exclude` | `["**/README.md"]` | Globs removed from every bucket hash and every gate key. `exclude = []` removes nothing. |

## `[registry]`

| Key | Meaning |
| --- | --- |
| `url` | Required. Registry host and namespace, such as `registry.example.com/myorg`. An image is `<url>/<bucket>:<sha>`. |

## `[runners]`

```toml
[runners]
unittest = { cmd = "bin/test", files = "unittest-files" }
e2e      = { cmd = "bin/e2e-test" }
```

Keyed by items name, not by tag. `changemap explain` renders `<cmd> --dirs <items>`, plus
`--files <list>` when `files` names a file-set.

| Key | Meaning |
| --- | --- |
| `cmd` | Required. The runner command. |
| `files` | A `files = true` table that feeds this runner's `--files`. A name that is no file-set is an error. |

## `[image.<bucket>]`

```toml
[image.astrochat]
dockerfile = "frontend/Dockerfile"
secrets    = ["sentry_auth_token"]
```

The key must name a SHA bucket. A bucket with no `[image.*]` table still hashes, but
`build-matrix` and `image build` skip it.

| Key | Default | Meaning |
| --- | --- | --- |
| `dockerfile` | required | Path from the repo root. |
| `context` | `"."` | Build context. |
| `platform` | none | Passed to `--platform` with `image build --load`. |
| `cache` | `true` | Use a registry layer cache, `<image>:buildcache`, when pushing. |
| `args` | `{}` | Extra `--build-arg` values. `PACKAGE_VERSION=<sha>` is always passed. |
| `secrets` | `[]` | Buildx secret ids. Each is passed as `--secret id=x,env=X` only when `X` is set. |

## `[k8s.<bucket>]`

```toml
[k8s.smartypants]
deployments = ["smartypants", "smartypants-worker"]
container   = "server"
```

The key must name a SHA bucket. `tangier deploy` reads it to roll out and roll back.

| Key | Default | Meaning |
| --- | --- | --- |
| `deployments` | `[<bucket>]` | Deployments that run this image. |
| `container` | `"server"` | The container that carries the image tag. |
| `version_var` | `<BUCKET>_VERSION` | The variable the manifests reference. |

The version variable is the bucket name upper-cased, with `-` as `_`, plus `_VERSION`:
`astronort-lector` gives `ASTRONORT_LECTOR_VERSION`.

## `[deploy]`

```toml
[deploy]
after = "bin/sentry-release ${ENV}"

[deploy.uat]
namespace     = "myorg-uat"
overlay       = "k8s/overlays/uat"
migration_job = "core-migrate-${CORE_VERSION}"
migration_version_bucket = "core"

[deploy.rollout]
max_wait = 600
```

`after` and `rollout` are reserved names. Every other `[deploy.<env>]` table is an environment:

| Key | Default | Meaning |
| --- | --- | --- |
| `namespace` | required | The k8s namespace. |
| `overlay` | required | The kustomize overlay to render. |
| `migration_timeout` | `600` | Seconds to wait for the migration Job. |
| `migration_job` | none | The migration Job's name. `${...}` takes version variables. |
| `migration_version_bucket` | none | The bucket whose prior version a rollback migrates back to. |

`[deploy.rollout]`, shared by every environment:

| Key | Default | Meaning |
| --- | --- | --- |
| `max_wait` | `600` | Seconds to wait for the rollout. |
| `poll_interval` | `10` | Seconds between polls. |
| `crash_threshold` | `3` | Restarts that count a pod as crash-looping. |
| `rollback_migration_timeout` | `600` | Seconds to wait for the rollback migration. |

`after` runs once a deploy has fully rolled out, never after a rollback. The string form means
`fatal = false`. The table form is `[deploy.after]` with `cmd` and `fatal`. With `fatal = true`, a
failed hook fails the deploy. `${ENV}` and each version variable are substituted per argument.

## `[tailnet]`

```toml
[tailnet]
operator = "tailscale-operator"

[tailnet.uat]
tag = "tag:myorg-uat-deploy"
```

| Key | Default | Meaning |
| --- | --- | --- |
| `operator` | `"tailscale-operator"` | The Tailscale Kubernetes operator's hostname. |
| `[tailnet.<env>] tag` | required | The tailnet ACL tag a deploy to `<env>` authenticates as. |

A `[tailnet.<env>]` that names no `[deploy.<env>]` warns, when any deploy environment exists.

## `[gate.<name>]`

```toml
[gate.test-backend]
cmd   = ["bin/test --dirs {unittest-items} --files {unittest-files}"]
env   = { TEST_DB = "1" }
scope = ["smartypants", "test-backend-inputs"]
```

| Key | Meaning |
| --- | --- |
| `cmd` | Required. A command, or a list run in order. |
| `env` | A table of strings added to each command's environment. A key input. |
| `scope` | Required. Packages whose content is a key input: SHA buckets, or tags with `paths`. |

Rules:

- A name uses letters, digits, `-` and `_`, and does not start with `-`.
- A table without `cmd` is a group. It holds `env`, `scope` and member tables only, one level deep.
  A member is `[gate.<group>.<member>]`, full name `<group>.<member>`. The member's `env` wins over
  the group's. The group's `scope` comes first. A member cannot be named `lock`.
- Output names turn `.` into `-`. Two names that give the same output are an error.
- A scope entry that is a file-set, names nothing, or is a tag with no `paths` is an error.
- A scope bucket brings in every tag in that bucket, plus each tag's transitive `depends`.

### Placeholders

A placeholder is a whole argument:

| Placeholder | Becomes |
| --- | --- |
| `{<name>-items}` | The items list `<name>`, comma-joined. |
| `{<file-set>}` | The file-set's selected files, comma-joined. |

An empty list is an empty argument. A placeholder inside a longer argument, or one that names no
list, is an error.

### Commands run without a shell

`cmd` and `[deploy] after` split into arguments at parse time and run without a shell. A token
`&&`, `||`, `|`, `;`, `>`, `>>`, `<` or `&` is an error. So is `$(` or a backtick anywhere. Put
that logic in a script and call the script.
