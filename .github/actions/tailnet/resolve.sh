#!/bin/bash
# Resolve the tailnet tag, operator and Tailscale version for the tailnet and
# deploy actions. Both run this file from their own checkout, so a caller who
# pins either action at a ref gets this script at that same ref.
#
# Reads INPUT_TAG, INPUT_ENV, INPUT_OPERATOR and INPUT_VERSION; writes tag,
# operator and version to $GITHUB_OUTPUT.
#
# Read with inline tomllib rather than through the CLI: four of the eight
# call sites want the tailnet connect ALONE, and making them install uv
# and tangier to read one string would defeat the point of the action.
set -euo pipefail
# Precedence: explicit input, then config. An explicit `tag` also means
# `pipeline.toml` need not exist at all — which is how a repo with no
# images (k8s-cluster is helm and kubectl) uses this action without a
# config file that would misrepresent it.
TAG="$INPUT_TAG"
OPERATOR="$INPUT_OPERATOR"

if [ -f pipeline.toml ]; then
  # Two lines out, so an absent value stays distinguishable from an
  # empty one without quoting games.
  CFG="$(INPUT_ENV="$INPUT_ENV" python3 -c '
import os, tomllib
with open("pipeline.toml", "rb") as fh:
    cfg = tomllib.load(fh).get("tailnet", {})
env = os.environ.get("INPUT_ENV", "")
print((cfg.get(env) or {}).get("tag", "") if env else "")
print(cfg.get("operator", "tailscale-operator"))
')"
  CFG_TAG="$(printf '%s' "$CFG" | sed -n 1p)"
  CFG_OPERATOR="$(printf '%s' "$CFG" | sed -n 2p)"
  TAG="${TAG:-$CFG_TAG}"
  OPERATOR="${OPERATOR:-$CFG_OPERATOR}"
fi
OPERATOR="${OPERATOR:-tailscale-operator}"

if [ -z "$TAG" ]; then
  echo "::error::no tailnet tag for env [${INPUT_ENV:-<unset>}]. Add a [tailnet.<env>] table to pipeline.toml, or pass the tag input." >&2
  exit 1
fi
# The version default is the one place the Tailscale pin lives.
# docs/actions/tailnet.md and the skill reference state it too; move them with
# it. The OIDC exchange needs 1.90.1 or later.
{
  echo "tag=$TAG"
  echo "operator=$OPERATOR"
  echo "version=${INPUT_VERSION:-1.104.4}"
} >> "$GITHUB_OUTPUT"
echo "Connecting as $TAG via $OPERATOR"
