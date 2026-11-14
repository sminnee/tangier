#!/bin/bash
# Install regctl at REGCTL_VERSION, for the build and deploy actions.
#
# Pinned by version, never `releases/latest`: a moving binary in the hot path
# of every build is a silent behaviour change with no PR anywhere. `pipefail`
# matters: without it a failed curl leaves a zero-byte file that fails
# confusingly several steps later.
#
# Into $RUNNER_TEMP rather than /usr/local/bin: that directory is writable on
# GitHub-hosted images, but not on a self-hosted runner or a container job
# running as non-root.
set -euo pipefail
mkdir -p "$RUNNER_TEMP/bin"
curl -fsSL -o "$RUNNER_TEMP/bin/regctl" \
  "https://github.com/regclient/regclient/releases/download/${REGCTL_VERSION}/regctl-linux-amd64"
chmod 755 "$RUNNER_TEMP/bin/regctl"
echo "$RUNNER_TEMP/bin" >>"$GITHUB_PATH"
"$RUNNER_TEMP/bin/regctl" version
