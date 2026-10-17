"""Budget kill switch: scale every service in the platform's ECS cluster to zero.

Invoked by SNS when the monthly AWS Budget reaches 100% of actual spend
(terraform/bootstrap/cost.tf). Scaling to zero stops compute and data transfer, the
costs that grow with use; the load balancer and NAT gateway keep running (a small, fixed
amount per day) until someone runs `make down`. `make resume` restores the services.

Idempotent and safe to invoke by hand: `make test-killswitch`.
"""

import json
import os
from typing import Any, Protocol


class EcsClient(Protocol):
    def get_paginator(self, operation_name: str) -> Any: ...
    def update_service(self, **kwargs: Any) -> Any: ...


class SnsClient(Protocol):
    def publish(self, **kwargs: Any) -> Any: ...


def scale_to_zero(ecs: EcsClient, cluster: str) -> list[str]:
    """Set desiredCount=0 on every service in `cluster`; return the service names."""
    names: list[str] = []
    try:
        pages = list(ecs.get_paginator("list_services").paginate(cluster=cluster))
    except Exception as exc:  # no cluster (e.g. after `make down`): nothing to stop
        if type(exc).__name__ == "ClusterNotFoundException":
            return names
        raise
    for page in pages:
        for arn in page.get("serviceArns", []):
            ecs.update_service(cluster=cluster, service=arn, desiredCount=0)
            names.append(arn.rsplit("/", 1)[-1])
    return names


def describe_trigger(event: dict[str, Any]) -> str:
    records = event.get("Records") or []
    if records and isinstance(records[0], dict) and "Sns" in records[0]:
        return records[0]["Sns"].get("Message", "")[:2000]
    return json.dumps(event)[:2000]


def handler(
    event: dict[str, Any],
    context: object,
    ecs: EcsClient | None = None,
    sns: SnsClient | None = None,
) -> dict[str, Any]:
    if ecs is None or sns is None:
        import boto3  # available in the Lambda runtime; tests pass fakes instead

        ecs = ecs or boto3.client("ecs")
        sns = sns or boto3.client("sns")
    cluster = os.environ["CLUSTER_NAME"]
    stopped = scale_to_zero(ecs, cluster)
    sns.publish(
        TopicArn=os.environ["ALERT_TOPIC_ARN"],
        Subject=f"zip-platform kill switch: {len(stopped)} service(s) scaled to zero",
        Message=(
            f"The monthly budget was reached, so every service in {cluster} was scaled to "
            f"zero: {', '.join(stopped) or 'none were running'}.\n\n"
            "Fixed costs (load balancer, NAT gateway) continue until `make down`.\n"
            "Restore the services with `make resume`.\n\n"
            f"Trigger:\n{describe_trigger(event)}"
        ),
    )
    return {"cluster": cluster, "stopped": stopped}
