---
name: tangier
description: Work with tangier, the pipeline.toml CI and deploy toolkit. Use in a repo with a pipeline.toml before a PR push, when running or choosing its gates (`[gate.*]` tables), or when a gate runs that you expected to be verified.
---

# tangier

tangier reads one `pipeline.toml` and answers CI questions from it: which parts a diff touches,
which image tags to build and deploy, and which gates have already passed.

Run tangier the way the project's own instructions or CI workflow do: `tangier`, `uvx
tangier@<version>`, or `python3 -m tangier`. `tangier <command> --help` lists each command's flags.

When the repo's `pipeline.toml` has `[gate.*]` tables, read [gate.md](gate.md) before a PR push,
before running or choosing gates, and when a gate runs that you expected to be verified.
