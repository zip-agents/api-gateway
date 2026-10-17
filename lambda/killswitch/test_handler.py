"""Unit tests for the kill switch. Run with `make test-lambda` (standard library only)."""

import os
import unittest
from typing import Any
from unittest import mock

import handler


class ClusterNotFoundException(Exception):
    pass


class FakeEcs:
    def __init__(self, pages: list[list[str]] | None = None, missing: bool = False) -> None:
        self.pages = pages or []
        self.missing = missing
        self.updates: list[dict[str, Any]] = []

    def get_paginator(self, operation_name: str) -> "FakeEcs":
        assert operation_name == "list_services"
        return self

    def paginate(self, cluster: str) -> list[dict[str, Any]]:
        if self.missing:
            raise ClusterNotFoundException(cluster)
        return [{"serviceArns": arns} for arns in self.pages]

    def update_service(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)


class FakeSns:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def publish(self, **kwargs: Any) -> None:
        self.messages.append(kwargs)


ENV = {"CLUSTER_NAME": "zip-platform-prod", "ALERT_TOPIC_ARN": "arn:aws:sns:eu-west-2:1:alerts"}
ARN = "arn:aws:ecs:eu-west-2:1:service/zip-platform-prod/"


@mock.patch.dict(os.environ, ENV)
class HandlerTest(unittest.TestCase):
    def test_scales_every_service_on_every_page_to_zero(self) -> None:
        ecs, sns = FakeEcs([[ARN + "gateway", ARN + "claude"], [ARN + "grok"]]), FakeSns()
        result = handler.handler({}, None, ecs=ecs, sns=sns)
        self.assertEqual(result["stopped"], ["gateway", "claude", "grok"])
        self.assertEqual(
            ecs.updates,
            [
                {"cluster": "zip-platform-prod", "service": ARN + name, "desiredCount": 0}
                for name in ("gateway", "claude", "grok")
            ],
        )
        self.assertEqual(len(sns.messages), 1)
        self.assertIn("3 service(s)", sns.messages[0]["Subject"])
        self.assertEqual(sns.messages[0]["TopicArn"], ENV["ALERT_TOPIC_ARN"])

    def test_missing_cluster_is_not_an_error(self) -> None:
        sns = FakeSns()
        result = handler.handler({}, None, ecs=FakeEcs(missing=True), sns=sns)
        self.assertEqual(result["stopped"], [])
        self.assertIn("none were running", sns.messages[0]["Message"])

    def test_budget_message_is_quoted_in_the_notification(self) -> None:
        sns = FakeSns()
        event = {"Records": [{"Sns": {"Message": "AWS Budget Notification: 100% reached"}}]}
        handler.handler(event, None, ecs=FakeEcs(), sns=sns)
        self.assertIn("AWS Budget Notification: 100% reached", sns.messages[0]["Message"])

    def test_other_errors_propagate(self) -> None:
        class Broken(FakeEcs):
            def paginate(self, cluster: str) -> list[dict[str, Any]]:
                raise PermissionError("AccessDenied")

        with self.assertRaises(PermissionError):
            handler.handler({}, None, ecs=Broken(), sns=FakeSns())


if __name__ == "__main__":
    unittest.main()
