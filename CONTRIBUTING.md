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

To release, open Actions → **Prepare release** → **Run workflow** on `main`. Pick `patch`, `minor`
or `major`, or give an exact version, and say why the version moves. The run:

1. Checks that `main`'s head commit is not already released.
2. Bumps the version with `bin/release-bump`, commits `chore: version X.Y.Z`, and pushes it to
   `release/vX.Y.Z`.
3. Opens a version PR whose body previews the release notes since the last tag, and dispatches CI
   on the branch.

Merging the version PR is the deliberate act: it moves the `@v0` alias that the Actions pin, which
ships to every consumer at once. When CI passes on `main`, `.github/workflows/release.yaml`
publishes the version to PyPI, tags `vX.Y.Z`, moves `@v0`, creates the GitHub Release, and deletes
the branch. Close the PR to call the release off; the next run overwrites its branch. A version PR
with no checks is fixed by running Prepare release again. A branch that holds a commit of its own is
never overwritten: merge or close its PR and delete the branch first.
`bin/release-bump <patch|minor|major|X.Y.Z>` makes the same bump locally for a hand-made version PR,
and `bin/release-plan [<commit>]` shows whether a commit releases.

A failed release is fixed by re-running the failed jobs. Every step is idempotent, and the release
job plans again before it publishes, so a stale re-run releases nothing.

One-time setup:

- The workflow uses trusted publishing, so the repo holds no PyPI token. Register the publisher on
  pypi.org: project `tangier`, repository `sminnee/tangier`, workflow `release.yaml`, environment
  `pypi`.
- Allow GitHub Actions to create pull requests: Settings → Actions → General → Workflow
  permissions.
- Limit the `pypi` environment's deployment branches to `main`. PyPI trusts any run of
  `release.yaml` on that environment, so without the limit a pushed branch could publish. Required
  reviewers are then optional: with them, each release also waits for that approval after the merge.

### Pre-releases (not yet supported)

The design, for when they are needed:

- **Inputs:** a `pre` input of `none`, `a`, `b` or `rc`, and a `bump` value `release`. `minor` with
  `rc` from 0.3.2 gives 0.4.0rc1, and running `rc` again gives 0.4.0rc2. Moving from `b` to `rc`
  resets the number. `release` drops the suffix, so 0.4.0rc2 becomes 0.4.0. These are the PEP 440
  forms PyPI needs.
- **`bin/release_version.py`:** accept the suffix in `parse`, with a sort key that ranks 0.4.0rc1
  below 0.4.0. `bin/release-plan` works out the highest version from final releases only. A pre-release must
  be above the highest final release and above any earlier pre-release of the same base.
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
