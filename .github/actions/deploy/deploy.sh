#!/bin/bash
# Run `tangier deploy` for the action's Deploy step.
#
# The aws-ssm tunnel's kubeconfig and keys arrive under TUNNEL_ names and are
# exported only when the tunnel opened, so empty values never shadow the
# kubeconfig a `none` caller selected. They never reach $GITHUB_ENV.
#
# `set -e` is a behaviour change from the bash this replaces, which fell
# through a failing summary into the real deploy. If it proves annoying, the
# relief valve is `continue-on-error: true` on a separate summary step, not
# dropping `set -e`.
set -euo pipefail
if [ -n "${TUNNEL_KUBECONFIG:-}" ]; then
  export KUBECONFIG="$TUNNEL_KUBECONFIG"
  export AWS_ACCESS_KEY_ID="$TUNNEL_AWS_ACCESS_KEY_ID"
  export AWS_SECRET_ACCESS_KEY="$TUNNEL_AWS_SECRET_ACCESS_KEY"
  export AWS_SESSION_TOKEN="$TUNNEL_AWS_SESSION_TOKEN"
fi
unset TUNNEL_KUBECONFIG TUNNEL_AWS_ACCESS_KEY_ID TUNNEL_AWS_SECRET_ACCESS_KEY TUNNEL_AWS_SESSION_TOKEN
# `--summary` writes the build table to $GITHUB_STEP_SUMMARY before anything is
# applied: pass 1 overwrites the deployed tags it reads, and the table is most
# wanted when the deploy then fails.
tangier deploy "$DEPLOY_ENV" --summary
