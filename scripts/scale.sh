#!/usr/bin/env bash
# Pause or resume every service without changing infrastructure.
#
#   scripts/scale.sh pause    every service to 0 tasks: compute and data transfer stop
#   scripts/scale.sh resume   back to each service's configured count
#   scripts/scale.sh status   desired and running counts
#
# Terraform ignores desired_count after creation, so deploys never undo a pause. The kill
# switch Lambda does the same as `pause`.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLUSTER="${CLUSTER:-zip-platform-prod}"
aws() { "$ROOT/scripts/aws.sh" "$@" | tr -d '\r'; }
action="${1:?usage: scale.sh pause|resume|status}"

services="$(aws ecs list-services --cluster "$CLUSTER" --query 'serviceArns' --output text)"
[ -n "$services" ] && [ "$services" != "None" ] || { echo "no services in $CLUSTER"; exit 0; }

for arn in $services; do
  name="${arn##*/}"
  case "$action" in
    pause)
      aws ecs update-service --cluster "$CLUSTER" --service "$arn" --desired-count 0 >/dev/null
      echo "$name: 0"
      ;;
    resume)
      count="$(aws ecs list-tags-for-resource --resource-arn "$arn" \
        --query "tags[?key=='zip-platform:desired-count'].value | [0]" --output text)"
      [[ "$count" =~ ^[0-9]+$ ]] || count=1
      aws ecs update-service --cluster "$CLUSTER" --service "$arn" --desired-count "$count" >/dev/null
      echo "$name: $count"
      ;;
    status)
      aws ecs describe-services --cluster "$CLUSTER" --services "$arn" \
        --query 'services[0].[serviceName,desiredCount,runningCount]' --output text
      ;;
    *) echo "unknown action: $action" >&2; exit 1 ;;
  esac
done
