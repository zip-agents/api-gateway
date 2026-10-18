#!/usr/bin/env bash
# Destroy everything that costs money, for good. Run by .github/workflows/nuke.yml (the
# platform deploy role) once the NUKE_AFTER date passes or the budget is spent, or by
# hand with administrator credentials.
#
# What it removes, in order:
#   1. every ECS service scaled to 0 at once (compute stops even if a later step fails)
#   2. the zip services, which deploy from their own repositories: ECS service, Cloud Map
#      service, log group and IAM roles, deleted directly (there is no coming back, so
#      their Terraform state is not needed)
#   3. gateway/deploy, then terraform/prod (load balancer, NAT gateway, Aurora, Cognito, ...)
#   4. every image in the ECR repositories
#
# What stays: the bootstrap stack (state bucket, empty ECR repositories, CI roles, budget,
# kill switch), which costs nothing to keep, and the Route 53 zone and domain.
# docs/deployment.md, "Switching it off", has the manual commands to remove those too.
#
# Safe to re-run: every step skips what is already gone.
set -euo pipefail

: "${AWS_REGION:?}"
PROJECT=zip-platform
CLUSTER="$PROJECT-prod"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
account="$(aws sts get-caller-identity --query Account --output text)"
bucket="$PROJECT-tfstate-$account-$AWS_REGION"

say() { echo "== $*"; }

cluster_active() {
  [ "$(aws ecs describe-clusters --clusters "$CLUSTER" \
        --query 'clusters[0].status' --output text 2>/dev/null)" = ACTIVE ]
}

services=()
if cluster_active; then
  for arn in $(aws ecs list-services --cluster "$CLUSTER" --query 'serviceArns' --output text); do
    services+=("${arn##*/}")
  done
fi

say "1. scaling every service to 0"
for name in "${services[@]}"; do
  aws ecs update-service --cluster "$CLUSTER" --service "$name" --desired-count 0 >/dev/null
  echo "   $name: 0"
done

say "2. removing the zip services"
for name in "${services[@]}"; do
  [ "$name" = gateway ] && continue
  aws ecs delete-service --cluster "$CLUSTER" --service "$name" --force >/dev/null
  aws ecs wait services-inactive --cluster "$CLUSTER" --services "$name"
  echo "   $name: ECS service deleted"
done
namespace="$(aws servicediscovery list-namespaces \
  --query "Namespaces[?Name=='zip.internal'].Id | [0]" --output text)"
if [ "$namespace" != None ]; then
  for id in $(aws servicediscovery list-services \
      --filters "Name=NAMESPACE_ID,Values=$namespace,Condition=EQ" \
      --query 'Services[].Id' --output text); do
    # ECS deregisters the instances asynchronously; the delete fails until it has.
    for attempt in $(seq 1 30); do
      aws servicediscovery delete-service --id "$id" 2>/dev/null && break
      [ "$attempt" -lt 30 ] || aws servicediscovery delete-service --id "$id" # show the error
      sleep 10
    done
    echo "   Cloud Map service $id deleted"
  done
fi
for name in claude grok dsh; do
  if aws logs describe-log-groups --log-group-name-prefix "/$PROJECT/prod/$name" \
       --query 'logGroups[0].logGroupName' --output text | grep -q "^/$PROJECT"; then
    aws logs delete-log-group --log-group-name "/$PROJECT/prod/$name"
    echo "   $name: log group deleted"
  fi
  for role in "$CLUSTER-$name-execution" "$CLUSTER-$name-task"; do
    aws iam get-role --role-name "$role" >/dev/null 2>&1 || continue
    for policy in $(aws iam list-role-policies --role-name "$role" --query 'PolicyNames' --output text); do
      aws iam delete-role-policy --role-name "$role" --policy-name "$policy"
    done
    aws iam delete-role --role-name "$role"
    echo "   $role deleted"
  done
done

destroy() {
  local stack="$1" key="$2"
  shift 2
  if ! aws s3api head-object --bucket "$bucket" --key "$key" >/dev/null 2>&1; then
    echo "   $stack: no state, nothing to destroy"
    return
  fi
  terraform -chdir="$ROOT/$stack" init -input=false \
    -backend-config="bucket=$bucket" -backend-config="region=$AWS_REGION"
  terraform -chdir="$ROOT/$stack" destroy -input=false -auto-approve "$@"
}

say "3. destroying gateway/deploy, then terraform/prod"
TF_VAR_region="$AWS_REGION" destroy gateway/deploy services/gateway/prod.tfstate -var image_tag=nuke
destroy terraform/prod platform/prod.tfstate

say "4. deleting every image in ECR"
for repo in $(aws ecr describe-repositories \
    --query "repositories[?starts_with(repositoryName, '$PROJECT/')].repositoryName" --output text); do
  ids="$(aws ecr list-images --repository-name "$repo" --query 'imageIds[*]' --output json)"
  [ "$ids" = "[]" ] && continue
  aws ecr batch-delete-image --repository-name "$repo" --image-ids "$ids" >/dev/null
  echo "   $repo: images deleted"
done

say "done: nothing that costs money is left except the Route 53 zone and the domain"
