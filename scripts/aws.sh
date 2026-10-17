#!/usr/bin/env bash
# Run the AWS CLI in a container with your local credentials (~/.aws, AWS_PROFILE, or
# AWS_ACCESS_KEY_ID/...). Nothing to install on the host but Docker.
#
#   scripts/aws.sh sts get-caller-identity
set -euo pipefail

ENGINE="${ENGINE:-docker}"
IMAGE="${AWS_CLI_IMAGE:-amazon/aws-cli:2.37.0}"
: "${AWS_REGION:?not set: cp platform.env.example platform.env, then set AWS_REGION and AWS_PROFILE in it}"

mkdir -p "$HOME/.aws"
# Run as you, so files written to the mounts are yours. A rootless engine already maps
# container root to you; --user there would pick an unmapped uid that cannot write them.
as_me=(--user "$(id -u):$(id -g)")
"$ENGINE" info --format '{{.SecurityOptions}}' 2>/dev/null | grep -q rootless && as_me=()
tty=()
[ -t 0 ] && tty=(-t)
exec "$ENGINE" run --rm -i "${tty[@]}" \
  "${as_me[@]}" -e HOME=/tmp \
  -v "$HOME/.aws:/aws" -e AWS_CONFIG_FILE=/aws/config -e AWS_SHARED_CREDENTIALS_FILE=/aws/credentials \
  -e AWS_PROFILE -e AWS_REGION -e AWS_DEFAULT_REGION="$AWS_REGION" \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
  -v "$PWD:/work" -w /work \
  "$IMAGE" "$@"
