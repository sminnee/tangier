#!/bin/bash
# Log in to each ECR host with the role's keys, for the build action. Reads
# ECR_HOSTS and AWS_REGION from registry-login.sh's outputs, and the AWS keys
# from configure-aws-credentials.
#
# The login lands in ~/.docker/config.json like the password login, so buildx
# and regctl pick it up. One token serves every ECR host in the region.
set -euo pipefail
TOKEN="$(aws ecr get-login-password --region "$AWS_REGION")"
echo "::add-mask::$TOKEN"
for HOST in $ECR_HOSTS; do
  echo "Logging in to $HOST"
  printf '%s' "$TOKEN" | docker login "$HOST" --username AWS --password-stdin
done
