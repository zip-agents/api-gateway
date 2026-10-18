#!/usr/bin/env bash
# Take the platform down.
#
#   scripts/teardown.sh down   destroy every service stack, then terraform/prod.
#                              Left: state bucket, ECR images, budget, kill switch (~$1/month).
#   scripts/teardown.sh nuke   `down`, then the bootstrap stack too: nothing Terraform
#                              created remains. Left: the domain registration and its
#                              hosted zone (printed below).
#
# Service stacks are destroyed from gateway/deploy (in this repository) and from the zip
# services' sibling checkouts (claude/, ...), which must exist. Asks for confirmation
# unless FORCE=1.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
action="${1:?usage: teardown.sh down|nuke}"
tf() { "$ROOT/scripts/tf.sh" "$@"; }
SERVICES=(gateway claude grok dsh-qwen3.8)

if [ -z "${FORCE:-}" ]; then
  read -r -p "This destroys the platform ($action) in ${AWS_REGION}. Type '$action' to continue: " answer
  [ "$answer" = "$action" ] || { echo "aborted"; exit 1; }
fi

for dir in "${SERVICES[@]}"; do
  if [ -f "$ROOT/$dir/deploy/main.tf" ]; then
    echo "== destroying service stack $dir/deploy"
    tf "$dir/deploy" init -input=false
    tf "$dir/deploy" destroy -auto-approve -var image_tag=teardown
  else
    echo "== $dir: no deploy/ stack here, skipped (check the service is not deployed)"
  fi
done

echo "== destroying terraform/prod"
tf terraform/prod init -input=false
tf terraform/prod destroy -auto-approve

[ "$action" = "nuke" ] || { echo "== down: done. To bring it back: make aws-apply, then re-run each service's deploy workflow."; exit 0; }

echo "== moving bootstrap state back to local, then destroying the bootstrap stack"
OVERRIDE="$ROOT/terraform/bootstrap/local_override.tf"
tf terraform/bootstrap init -input=false
printf 'terraform {\n  backend "local" {}\n}\n' >"$OVERRIDE"
trap 'rm -f "$OVERRIDE"' EXIT
TF_NO_BACKEND_CONFIG=1 tf terraform/bootstrap init -input=false -migrate-state -force-copy
tf terraform/bootstrap destroy -auto-approve
rm -f "$ROOT/terraform/bootstrap/terraform.tfstate" "$ROOT/terraform/bootstrap/terraform.tfstate.backup"

zone="$(sed -n 's/^hosted_zone_name *= *"\(.*\)".*/\1/p' "$ROOT/terraform/prod/terraform.tfvars" 2>/dev/null || true)"
cat <<EOF
== nuke: done. Two things are left, both by choice of AWS:
   1. The domain registration: turn off auto-renew in the Route 53 console
      (Registered domains -> ${zone:-<your domain>} -> Auto-renew: off).
   2. Its hosted zone (\$0.50/month). Delete it with:
      scripts/aws.sh route53 delete-hosted-zone --id \$(scripts/aws.sh route53 list-hosted-zones-by-name \\
        --dns-name ${zone:-<your domain>} --query 'HostedZones[0].Id' --output text)
      (Only the NS and SOA records may remain; everything Terraform created is gone.)
EOF
