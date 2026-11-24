#!/bin/bash
# Fail fast when the connection method's inputs are missing, before any
# network step. CONNECT is the method `tangier connect show` read from
# pipeline.toml.
#
# The runner does not enforce `required:` on a composite action's inputs, so
# an empty `client-id` would otherwise fail inside tailscale/github-action
# with a message that names neither the input nor the environment.
set -euo pipefail
case "$CONNECT" in
  tailnet)
    for input in client-id:"$INPUT_CLIENT_ID" audience:"$INPUT_AUDIENCE"; do
      if [ -z "${input#*:}" ]; then
        echo "::error::\`${input%%:*}\` is empty: $DEPLOY_ENV connects by tailnet, which needs \`client-id\` and \`audience\`" >&2
        exit 1
      fi
    done
    ;;
  none)
    if ! context=$(kubectl config current-context 2>/dev/null); then
      echo "::error::$DEPLOY_ENV connects by none, but kubectl has no current context. Set KUBECONFIG, or select a context, in an earlier step" >&2
      exit 1
    fi
    echo "kubectl context: $context"
    ;;
esac
