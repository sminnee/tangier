#!/bin/bash
# Install the tangier CLI for the build and deploy actions. Both run this file
# from their own checkout, so a caller who pins either action at a ref gets
# this script at that same ref. CI's build-action job covers both callers.
#
# Reads INPUT_REF and ACTION_REF. `github.action_ref` is empty inside a `run:`
# body but correct when bound through `env:` (actions/runner#2473), so the
# caller binds it. It is also empty for a local `uses: ./...`, which is how
# tangier tests its own actions — hence the input, and a hard error rather
# than a silent fallback to `main`.
set -euo pipefail
REF="${INPUT_REF:-${ACTION_REF:-}}"
if [ -z "$REF" ]; then
  echo "::error::cannot determine which tangier ref to install. Pass the tangier-ref input when calling this action by local path." >&2
  exit 1
fi
# `uv tool install`, not `uvx --from`, so `tangier` stays on PATH for any repo
# script that runs after the action.
uv tool install "git+https://github.com/sminnee/tangier@${REF}"
