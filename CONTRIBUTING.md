# Contributing to tangier

## Development

```sh
bin/test                  # stdlib unittest, no dependencies; one Python, any tree
tangier gate run test     # the suite on 3.11, 3.12 and 3.13, one member each; needs uv
tangier gate run lint     # lint.check and lint.format
tangier gate run no-dependencies  # no declared dependency, and no third-party import
tangier gate push         # sync records with origin before the PR push, so CI reuses the passes
```

Releases are a maintainer step, not part of the everyday loop. `bin/release v0.2.0` tags a version
and moves the `@v0` alias that the Actions pin. It:

- refuses a dirty tree, a HEAD that is not `origin/main`, and a tag that differs from the
  `pyproject.toml` version;
- runs the tests;
- pushes nothing.

Pushing the version tag publishes it to PyPI through `.github/workflows/release.yaml`. The workflow
uses trusted publishing, so the repo holds no PyPI token. Register the publisher once on pypi.org:
project `tangier`, repository `sminnee/tangier`, workflow `release.yaml`, environment `pypi`.

`skills/tangier/` is the agent skill; the README shows how to link it. Link it from the checkout
that tracks `main`, so it follows that checkout.

`bin/parity-check <path-to-repo>` diffs `tangier changemap` against a repo's pre-extraction
`bin/changemap` across many refs, in throwaway worktrees, and is the gate for migrating a repo onto
tangier. It is deliberately not part of CI — it needs a checkout of the consuming repo.

See `docs/specs/changemap.md` for the resolution rules in detail.
