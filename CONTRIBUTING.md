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

To release, wait for CI to pass on `main`, then open Actions → **Release** → **Run workflow** on
`main`. Pick `patch`, `minor` or `major`, or give an exact version, and say why the version moves.
The run:

1. Checks that CI passed on `main`'s head commit, and that the commit is not already released.
2. Bumps the version with `bin/release-bump`, commits `chore: version X.Y.Z`, runs the core tests
   on it, and pushes it to `release/vX.Y.Z`. The run summary previews the release notes since the
   last tag.
3. Waits for approval on the `pypi` environment. Approving is the deliberate act: it moves the
   `@v0` alias that the Actions pin, which ships to every consumer at once.
4. Fast-forwards `main` to the version commit, publishes to PyPI, tags `vX.Y.Z`, moves `@v0`,
   creates the GitHub Release, and deletes the branch.

The version commit changes only the version strings, so `main`'s CI stands for it, and its push
starts no CI run. A commit that lands on `main` while the run waits makes step 4 refuse, before it
publishes: run the workflow again. A run that is rejected leaves its branch behind, and the next run
overwrites it.
`bin/release-bump <patch|minor|major|X.Y.Z>` makes the same bump locally, and
`bin/release-plan [<commit>]` shows whether a commit releases.

A failed release is fixed by re-running the failed jobs. Every step is idempotent, and the release
job plans again before it publishes, so a stale re-run releases nothing.

One-time setup:

- The workflow uses trusted publishing, so the repo holds no PyPI token. Register the publisher on
  pypi.org: project `tangier`, repository `sminnee/tangier`, workflow `release.yaml`, environment
  `pypi`.
- Give the `pypi` environment required reviewers, or nothing waits for approval.
- `main` must accept a push from `github-actions[bot]`. If a branch rule requires a PR or status
  checks, add the bot to its bypass list: the version commit has no CI run of its own.

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
