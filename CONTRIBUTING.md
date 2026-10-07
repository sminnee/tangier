# Contributing to tangier

## Development

```sh
bin/test                  # stdlib unittest, no dependencies; one Python, any tree
tangier gate run test     # the suite on 3.11, 3.12 and 3.13, one member each; needs uv
tangier gate run lint     # lint.check and lint.format
tangier gate run no-dependencies  # no declared dependency, and no third-party import
```

Each `gate run` publishes its passes to `origin`, so CI reuses them. If it warns, run
`tangier gate sync` before the PR push.

To release, bump `version` in both `pyproject.toml` and `tangier/__init__.py` in a PR, move the
`@vX.Y.Z` example in `skills/tangier/reference/github-actions.md`, and merge it.
When CI passes on `main`, `.github/workflows/release.yaml` publishes the version to PyPI, tags
`vX.Y.Z`, and moves the `@v0` alias that the Actions pin. Moving the alias ships to every consumer
at once, so the version PR is the deliberate act. `bin/release-plan [<commit>]` shows whether a
commit releases.

A failed release is fixed by re-running the workflow. Every step is idempotent, and the release job
plans again before it publishes, so a stale re-run releases nothing. The workflow uses
trusted publishing, so the repo holds no PyPI token. Register the publisher once on pypi.org:
project `tangier`, repository `sminnee/tangier`, workflow `release.yaml`, environment `pypi`.

`skills/tangier/` is the agent skill; the README shows how to link it. Link it from the checkout
that tracks `main`, so it follows that checkout.

`bin/parity-check <path-to-repo>` diffs `tangier changemap` against a repo's pre-extraction
`bin/changemap` across many refs, in throwaway worktrees, and is the gate for migrating a repo onto
tangier. It is deliberately not part of CI — it needs a checkout of the consuming repo.

See `docs/specs/changemap.md` for the resolution rules in detail.
