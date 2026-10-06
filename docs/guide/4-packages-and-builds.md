# 4. Packages and builds

A monorepo ships many images, and each should have its own version. tangier versions a package by
its **content**: an image tag is a hash of the files that go into it. A change rebuilds only the
images whose files changed, and an image that is already published is never rebuilt.

Keys are in the [`pipeline.toml` reference](../../skills/tangier/reference/pipeline-toml.md#imagebucket),
and commands in the [CLI reference](../../skills/tangier/reference/cli.md#image).

## SHA buckets

`sha = true` makes a tag a **SHA bucket**, a package with its own version:

```toml
[astronort-lector]
paths   = "service/lector/**"
depends = ["smartycore", "askastro-taskiq", "askastro-db"]
sha     = true
```

The bucket's hash covers its own paths plus the paths of every tag it depends on, transitively. A
change to `smartycore` therefore moves the hash of every service that uses it. `sha = "<bucket>"`
adds a tag's paths to another bucket instead.

```sh
tangier changemap sha astronort-lector   # 3f9c2a71b0
tangier changemap sha --all              # ASTRONORT_LECTOR_VERSION=3f9c2a71b0, one per bucket
```

## Files changed, not commits

The hash reads file content from `git ls-tree`, not commit SHAs. A commit that touches only docs,
or another service, leaves the bucket's hash alone. A revert gives back the old hash, and the old
image. A rebase or a squash changes no hash. Two branches with the same service content share one
image.

`[sha] exclude` removes files from every hash. It defaults to `["**/README.md"]`, so editing a
README never rebuilds an image.

A tag's own `exclude` also filters its bucket. askastro learned this the hard way: excluding
`**/*_test.py` from a tag that feeds a bucket stops the image rebuilding when a test file changes,
because the test file is in the image. The [setup checklist](../../skills/tangier/setup.md#add-or-change-a-tag)
states the rule.

A tag that feeds a bucket may use only literal paths and `dir/**` globs; see the
[glob rules](../../skills/tangier/reference/pipeline-toml.md#tags).

## Versions

Each bucket's version is an environment variable, such as `ASTRONORT_LECTOR_VERSION` for
`astronort-lector`. Kubernetes manifests reference these, and `tangier deploy` substitutes them.

Each image build also gets `--build-arg PACKAGE_VERSION=<hash>`, which a Dockerfile can use as its
release identifier.

## Images

`[image.<bucket>]` makes a bucket buildable:

```toml
[registry]
url = "registry.sminn.ee/tangerine"

[image.astrochat]
dockerfile = "frontend/Dockerfile"
secrets    = ["sentry_auth_token"]
```

```sh
tangier image tag astrochat           # the tag: the bucket's hash
tangier image build astrochat --push  # build and push, unless already published
tangier image build astrochat --load  # build into the local Docker daemon
tangier image build astrochat --print # print the docker command only
```

`image build --push` first asks the registry whether the tag exists. When it does, the build is
skipped. This is what makes a "build everything" step cheap: only the images whose content changed
do any work.

`tangier changemap build-matrix` lists the buckets a diff touches that have an `[image.*]` table,
as a JSON array for a CI matrix. A bucket reached only through `depends` is included, because its
hash moved.

## Running a whole stack at one commit

`image compose` fills a docker-compose template with the image refs for this commit:

```yaml
services:
  server:
    image: "[[smartypants]]"
```

```sh
tangier image compose docker-compose.tmpl.yml > docker-compose.yml
# image: "registry.sminn.ee/tangerine/smartypants:3f9c2a71b0"
```

An unchanged service resolves to the image already published for its hash. An e2e job can
therefore start the full stack while building only the images the change touched.

Next: [GitHub Actions](5-github-actions.md).
