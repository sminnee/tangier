#!/bin/bash
# Log in to the password registry, and work out which ECR hosts ecr-login.sh
# logs in to, for the build and deploy actions. Both actions run this file, and
# the other scripts here, from their own checkout, as with install-tangier.sh,
# so a caller who pins either action gets the scripts at that same ref.
#
# Reads:
#   MIRRORS            `true` logs in to `[registry] mirrors` too, `false` to
#                      `[registry] url` only.
#   REGISTRY_USERNAME  and REGISTRY_PASSWORD, for the one non-ECR registry.
#   AWS_ROLE_ARN       for the ECR registries, which must share one region.
#   REQUIRE            `true` fails when a registry's credential is missing (the
#                      build, which must push). `false` skips that host's login
#                      instead (the deploy, which only reads).
#
# Writes `ecr-hosts` and `ecr-region` to $GITHUB_OUTPUT. ECR's login waits for
# the role, which only a `uses:` step can assume.
#
# `docker login` takes only the host: `${URL%%/*}` gives `registry.sminn.ee`
# from `registry.sminn.ee/tangerine`, and keeps a `:port`. The credentials are
# checked here so a bad combination fails with a clear message, not later as a
# vague 401 from the registry.
set -euo pipefail
fail() {
  echo "::error::$1"
  exit 1
}
REQUIRE="${REQUIRE:-true}"
case "$REQUIRE" in
  true | false) ;;
  *) fail "REQUIRE must be true or false, not '$REQUIRE'" ;;
esac
ARGS=()
case "${MIRRORS:-}" in
  true) ;;
  false) ARGS+=(--no-mirrors) ;;
  *) fail "mirrors must be true or false, not '${MIRRORS:-}'" ;;
esac
URLS="$(tangier image registries "${ARGS[@]+"${ARGS[@]}"}")"
PASSWORD_HOST=""
ECR_HOSTS=""
ECR_REGION=""
while IFS= read -r URL; do
  HOST="${URL%%/*}"
  if [[ "$HOST" == *.dkr.ecr.*.amazonaws.com ]]; then
    # An ECR token is good only for its own region's hosts.
    REGION="${HOST#*.dkr.ecr.}"
    REGION="${REGION%.amazonaws.com}"
    if [ -n "$ECR_REGION" ] && [ "$ECR_REGION" != "$REGION" ]; then
      fail "ECR registries in $ECR_REGION and $REGION: every ECR registry must be in one region"
    fi
    ECR_REGION="$REGION"
    [[ " $ECR_HOSTS " == *" $HOST "* ]] || ECR_HOSTS="${ECR_HOSTS:+$ECR_HOSTS }$HOST"
  elif [ -z "$PASSWORD_HOST" ] || [ "$PASSWORD_HOST" = "$HOST" ]; then
    PASSWORD_HOST="$HOST"
  else
    fail "registries $PASSWORD_HOST and $HOST both need a password, but there is only one registry-password"
  fi
done <<<"$URLS"

if [ -n "$PASSWORD_HOST" ] && [ -z "${REGISTRY_PASSWORD:-}" ]; then
  [ "$REQUIRE" = "true" ] && fail "registry $PASSWORD_HOST needs registry-password"
  echo "::notice::No registry-password; not logging in to $PASSWORD_HOST"
  PASSWORD_HOST=""
elif [ -z "$PASSWORD_HOST" ] && [ -n "${REGISTRY_PASSWORD:-}" ]; then
  echo "::notice::No registry here needs registry-password; skipping that login"
fi
if [ -n "$ECR_HOSTS" ] && [ -z "${AWS_ROLE_ARN:-}" ]; then
  [ "$REQUIRE" = "true" ] && fail "ECR registry $ECR_HOSTS needs aws-role-arn"
  echo "::notice::No aws-role-arn; not logging in to $ECR_HOSTS"
  ECR_HOSTS=""
  ECR_REGION=""
elif [ -z "$ECR_HOSTS" ] && [ -n "${AWS_ROLE_ARN:-}" ]; then
  echo "::notice::No registry here is ECR; skipping the aws-role-arn login"
fi

if [ -n "$PASSWORD_HOST" ]; then
  echo "Logging in to $PASSWORD_HOST"
  printf '%s' "$REGISTRY_PASSWORD" | docker login "$PASSWORD_HOST" \
    --username "${REGISTRY_USERNAME:-registry}" --password-stdin
fi

{
  echo "ecr-hosts=$ECR_HOSTS"
  echo "ecr-region=$ECR_REGION"
} >>"$GITHUB_OUTPUT"
