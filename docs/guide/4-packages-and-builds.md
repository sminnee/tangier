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
tangier image tag astrochat                         # the tag: the bucket's hash
tangier image build astrochat --push                # build and push, unless already published
tangier image build astrochat smartypants --pull    # pull each image, or build it locally
tangier image build astrochat --pull --print        # print the docker command only
```

`image build` takes three settings:

1. **Which images.** One or more bucket names.
2. **Where they go.** `--push` publishes to the registry. `--pull` gets them into the local Docker
   daemon. One of the two is required.
3. **Reuse or rebuild.** By default, a bucket whose tag is already published is not built.
   `--force` builds every bucket.

| | reuse (default) | `--force` |
| --- | --- | --- |
| `--push` | Published on every registry: skip. Published on some: copy to the rest. Otherwise: build and push, reading and writing the cache. | Build and push, reading and writing the cache. |
| `--pull` | Published: `docker pull`. Otherwise: build into the local daemon, reading the cache only. | Build into the local daemon, reading the cache only. |

The reuse is what makes a "build everything" step cheap: only the images whose content changed do
any work. A pull request's e2e job can run `image build <every bucket> --pull` and build only the
images the change touched, reading the cache that `main`'s builds wrote.

A `--pull` build never writes the cache. Buildx keys a cached `RUN` layer by its command text, not
by what the command fetched, so a branch build could poison the cache that `main`'s builds reuse.

A bucket with uncommitted changes is never reused, because its tag hashes the committed tree, not
the files on disk. With `--pull` it is built, and its line ends `(uncommitted changes: <files>)`.
With `--push` it is an error, because the push would publish uncommitted content under a committed
hash.

Builds run two at a time. `--jobs 1` runs one at a time, for a machine short of memory.

`--buildx-arg=<arg>` passes an extra argument to `docker buildx build`, such as
`--buildx-arg=--provenance=false`. It repeats.

`[registry] mirrors` lists more registries that each image is pushed to as well. Only `url` is read
from. [Builds](5-github-actions.md#builds) shows how to move registries with it.

`tangier changemap build-matrix` lists the buckets a diff touches that have an `[image.*]` table,
as a JSON array for a CI matrix. A bucket reached only through `depends` is included, because its
hash moved.

### Multi-platform images

Buildx can only build several platforms in one run by emulating the ones the machine is not, which
is slow. Tangier builds each platform on a machine of its own, then joins them:

```sh
tangier image build astrochat --push --leg          # on an amd64 machine, and again on an arm64 one
tangier image merge astrochat --platforms linux/amd64,linux/arm64
```

A leg builds the Docker daemon's own platform and pushes it to `url` alone as
`:<tag>-<os>-<arch>`, such as `:3f9c2a71b0-linux-arm64`. It writes no `:latest` and no mirror, so
nothing pulls a one-platform image by the final tag. Each leg has its own cache,
`:buildcache-<os>-<arch>`, because two legs would overwrite one shared cache. A leg skips when the
final tag or its leg tag already holds its platform.

`image merge` joins the legs into one index under `:<tag>` and `:latest` on `url`, then copies both
to each mirror. It skips when every registry already has every platform, and copies `:<tag>` and
`:latest` from `url` when `url` has them all. An image published for one platform before has the same tag after, because the tag hashes
only source files. That platform's leg skips, and the merge takes it from the published tag. The
merge writes a new index over that tag, so its digest changes.

[Multi-platform images](5-github-actions.md#multi-platform-images) shows the jobs that run a leg on
each runner and then the merge.

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
