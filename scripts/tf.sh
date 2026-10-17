#!/usr/bin/env bash
# Run Terraform in a container against one stack, with your local AWS credentials.
# `init` is given the state bucket automatically.
#
#   scripts/tf.sh terraform/prod plan
#   scripts/tf.sh claude/deploy apply -var image_tag=abc123
#
# The repository root is mounted, so service stacks in the sibling checkouts (gateway/,
# claude/, ...) work too.
set -euo pipefail

ENGINE="${ENGINE:-docker}"
IMAGE="${TERRAFORM_IMAGE:-hashicorp/terraform:1.16.3}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STACK="${1:?usage: tf.sh <stack directory> <terraform arguments...>}"
shift
: "${AWS_REGION:?not set: cp platform.env.example platform.env, then set AWS_REGION and AWS_PROFILE in it}"

args=("$@")
if [ "${1:-}" = "init" ] && [ -z "${TF_NO_BACKEND_CONFIG:-}" ]; then
  account="$("$ROOT/scripts/aws.sh" sts get-caller-identity --query Account --output text | tr -d '\r')"
  args+=("-backend-config=bucket=zip-platform-tfstate-${account}-${AWS_REGION}"
         "-backend-config=region=${AWS_REGION}")
fi

mkdir -p "$HOME/.aws"
# Run as you, so files written to the mounts are yours. A rootless engine already maps
# container root to you; --user there would pick an unmapped uid that cannot write them.
as_me=(--user "$(id -u):$(id -g)")
"$ENGINE" info --format '{{.SecurityOptions}}' 2>/dev/null | grep -q rootless && as_me=()
tty=()
[ -t 0 ] && tty=(-t)
exec "$ENGINE" run --rm -i "${tty[@]}" \
  "${as_me[@]}" -e HOME=/tmp -e TF_IN_AUTOMATION=1 \
  -v "$HOME/.aws:/aws" -e AWS_CONFIG_FILE=/aws/config -e AWS_SHARED_CREDENTIALS_FILE=/aws/credentials \
  -e AWS_PROFILE -e AWS_REGION -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
  -e TF_VAR_region="$AWS_REGION" \
  -v "$ROOT:/work" -w "/work/$STACK" \
  "$IMAGE" "${args[@]}"
