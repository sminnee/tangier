# Contributing to tangier

## Development

```sh
bin/test                  # stdlib unittest, no dependencies; one Python, any tree
tangier gate run test --wait     # the tests the change reaches, on 3.11, 3.12 and 3.13; needs uv
tangier gate run lint --wait     # lint.check and lint.format
tangier gate run no-dependencies --wait  # no declared dependency, and no third-party import
```

The tests sit in one directory for each area under `tangier/tests/`. The `test` gate runs only the
directories a change reaches, through the `*-tests` and `*-src` tags in `pipeline.toml`. A new
module goes in the matching `*-src` tag, or no area's tests run when it changes.

Each `gate run` publishes its passes and failures to `origin`, so CI reuses the passes. If it
warns, run `tangier gate sync` before the PR push. When `pipeline.toml` uses a field the installed
tangier does not know yet, run the checkout's own: `python3 -m tangier gate run ...`.

## Releasing

The version lives in the git tags. The source holds the placeholder `0.0.0.dev0`, and the build
stamps the release into it.

To release, open Actions → **Release** → **Run workflow** on `main`. Pick `patch`, `minor` or
`major`, or give an exact version. The run:

1. Bumps the highest `vX.Y.Z` tag with `bin/release-version next`. It refuses when `main`'s head
   is already released.
2. Runs CI on `main`'s head, in full. `main` has no CI of its own: pull requests and releases are
   where the tests run.
3. Waits for the `pypi` environment, if it has required reviewers.
4. Stamps the version with `bin/release-version stamp`, builds, and publishes to PyPI.
5. Tags `vX.Y.Z`, moves the `@v0` alias that the Actions pin, and creates the GitHub Release with
   notes since the previous tag.

Running the workflow is the deliberate act: moving `@v0` ships to every consumer at once.

A failed release is fixed by re-running the failed jobs. Every publish step is idempotent, and a
tag already on the released commit is reused.

One-time setup:

- The workflow uses trusted publishing, so the repo holds no PyPI token. Register the publisher on
  pypi.org: project `tangier`, repository `sminnee/tangier`, workflow `release.yaml`, environment
  `pypi`.
- Limit the `pypi` environment's deployment branches to `main`. PyPI trusts any run of
  `release.yaml` on that environment, so without the limit a pushed branch could publish. Required
  reviewers are then optional: with them, each release also waits for that approval after CI.

### Pre-releases (not yet supported)

The design, for when they are needed:

- **Inputs:** a `pre` input of `none`, `a`, `b` or `rc`, and a `bump` value `release`. `minor` with
  `rc` from 0.3.2 gives 0.4.0rc1, and running `rc` again gives 0.4.0rc2. Moving from `b` to `rc`
  resets the number. `release` drops the suffix, so 0.4.0rc2 becomes 0.4.0. These are the PEP 440
  forms PyPI needs.
- **`bin/release-version`:** accept the suffix in `parse`, with a sort key that ranks 0.4.0rc1
  below 0.4.0. A pre-release must be above the highest final release and above any earlier
  pre-release of the same base.
- **Publishing:** a pre-release goes to PyPI, where pip and uv skip it unless asked, so a user tries
  one with `uvx tangier@0.4.0rc1`. It is tagged `v0.4.0rc1` and never moves `@v0`, so Actions
  users opt in by pinning the exact tag. Its GitHub Release is created with `--prerelease`.

## Other tools

`skills/tangier/` is the agent skill; the README shows how to link it. Link it from the checkout
that tracks `main`, so it follows that checkout.

`bin/parity-check <path-to-repo>` diffs `tangier changemap` against a repo's pre-extraction
`bin/changemap` across many refs, in throwaway worktrees, and is the gate for migrating a repo onto
tangier. It is deliberately not part of CI — it needs a checkout of the consuming repo.

See `docs/specs/changemap.md` for the resolution rules in detail.
