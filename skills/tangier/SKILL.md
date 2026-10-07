---
name: tangier
description: Work with tangier, which makes monorepo CI run only what a change needs. Use in a repo with a pipeline.toml when editing pipeline.toml (tags, depends, items, gates, images), writing or changing a CI workflow that calls tangier, adding tangier to a repo, running gates before a PR push, building or tagging images, or when a gate runs that you expected to be verified.
---

# tangier

tangier reads one `pipeline.toml` that maps a monorepo into tags. From a diff it works out which
tests, gates and image builds a change needs, and skips gates that already passed locally.

Run tangier the way the project's own instructions or CI workflow do: `tangier`, `uvx
tangier`, or `python3 -m tangier`. `tangier <command> --help` lists each command's flags.

| Task | Read |
| --- | --- |
| Push a branch, or run, choose or debug gates | [gate.md](gate.md) |
| Add tangier to a repo, or add or change a tag, items list, gate or image | [setup.md](setup.md) |
| Write or change a CI workflow, including a nightly or a deploy | [ci.md](ci.md) |
| Look up a `pipeline.toml` key | [reference/pipeline-toml.md](reference/pipeline-toml.md) |
| Look up a command, flag, output name or exit code | [reference/cli.md](reference/cli.md) |
| Copy the canonical workflow, or an action's inputs | [reference/github-actions.md](reference/github-actions.md) |
