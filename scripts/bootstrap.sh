#!/usr/bin/env bash
# Apply terraform/bootstrap with administrator credentials. Run once; re-running is safe.
#
# The state bucket is created by this stack, so the first run applies with local state,
# then migrates that state into the new bucket. Later runs use the bucket directly.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STACK=terraform/bootstrap
OVERRIDE="$ROOT/$STACK/local_override.tf"
tf() { "$ROOT/scripts/tf.sh" "$STACK" "$@"; }

[ -f "$ROOT/$STACK/terraform.tfvars" ] || {
  echo "Create $STACK/terraform.tfvars first (see terraform.tfvars.example)." >&2
  exit 1
}

account="$("$ROOT/scripts/aws.sh" sts get-caller-identity --query Account --output text | tr -d '\r')"
bucket="zip-platform-tfstate-${account}-${AWS_REGION}"

if "$ROOT/scripts/aws.sh" s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
  echo "== state bucket $bucket exists: applying with remote state"
  tf init -input=false
  tf apply
  exit 0
fi

echo "== first run: applying with local state, then moving it into $bucket"
printf 'terraform {\n  backend "local" {}\n}\n' >"$OVERRIDE"
trap 'rm -f "$OVERRIDE"' EXIT
TF_NO_BACKEND_CONFIG=1 tf init -input=false
tf apply

rm -f "$OVERRIDE"
tf init -input=false -migrate-state -force-copy
rm -f "$ROOT/$STACK/terraform.tfstate" "$ROOT/$STACK/terraform.tfstate.backup"
echo "== bootstrap state now lives in s3://$bucket/bootstrap.tfstate"
echo "== confirm the SNS subscription email AWS just sent, so alerts reach you"
