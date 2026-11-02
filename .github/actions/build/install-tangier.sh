#!/bin/bash
# Install the tangier CLI for the build and deploy actions. Both run this file
# from their own checkout, so a caller who pins either action at a ref gets
# this script at that same ref. CI's install-tangier job runs it each way.
#
# Reads INPUT_VERSION, a PyPI release, and INPUT_REF, a git ref for testing an
# unreleased change. With neither, it installs the latest PyPI release. The
# caller should pass the version its own `uvx` calls run: two versions can hash
# image tags apart.
set -euo pipefail
VERSION="${INPUT_VERSION:-}"
REF="${INPUT_REF:-}"
# `uv tool install`, not `uvx --from`, so `tangier` stays on PATH for any repo
# script that runs after the action. `--upgrade` so a persistent runner does
# not keep an older install.
if [ -n "$VERSION" ] && [ -n "$REF" ]; then
  echo "::error::pass tangier-version or tangier-ref, not both" >&2
  exit 1
elif [ -n "$REF" ]; then
  uv tool install --upgrade "git+https://github.com/sminnee/tangier@${REF}"
elif [ -n "$VERSION" ]; then
  uv tool install --upgrade "tangier==${VERSION}"
else
  uv tool install --upgrade tangier
fi
tangier --version
