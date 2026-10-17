# Cost controls. They live in the bootstrap stack so they outlive `make down`.
#
# AWS has no hard spending cap, and billing data lags by hours. So:
#   50% and 80% of the monthly budget (actual)  -> email
#   100% forecast                               -> email
#   100% actual                                 -> email, and the kill switch: a Lambda
#                                                  that scales every ECS service to zero
# The services have no autoscaling at all (terraform/modules/ecs-service), so compute has
# a fixed ceiling; scaling to zero also stops data transfer, the only usage-driven cost.

resource "aws_sns_topic" "alerts" {
  name = "${local.project}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email" # AWS emails a confirmation link once
  endpoint  = var.alert_email
}

resource "aws_sns_topic" "killswitch" {
  name = "${local.project}-killswitch"
}

data "aws_iam_policy_document" "killswitch_topic" {
  statement {
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.killswitch.arn]
    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "killswitch" {
  arn    = aws_sns_topic.killswitch.arn
  policy = data.aws_iam_policy_document.killswitch_topic.json
}

resource "aws_budgets_budget" "monthly" {
  name         = "${local.project}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = [50, 80]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alert_email]
    }
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
    subscriber_sns_topic_arns  = [aws_sns_topic.killswitch.arn]
  }

  depends_on = [aws_sns_topic_policy.killswitch]
}

# ------------------------------------------------------------------------- kill switch

data "archive_file" "killswitch" {
  type        = "zip"
  source_file = "${path.module}/../../lambda/killswitch/handler.py"
  output_path = "${path.module}/../../build/killswitch.zip"
}

data "aws_iam_policy_document" "killswitch_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "killswitch" {
  name               = "${local.project}-killswitch"
  assume_role_policy = data.aws_iam_policy_document.killswitch_trust.json
}

resource "aws_iam_role_policy_attachment" "killswitch_logs" {
  role       = aws_iam_role.killswitch.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "killswitch" {
  statement {
    actions   = ["ecs:ListServices"]
    resources = ["*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = ["arn:aws:ecs:${var.region}:${local.account_id}:cluster/${local.cluster_name}"]
    }
  }
  statement {
    actions   = ["ecs:UpdateService", "ecs:DescribeServices"]
    resources = ["arn:aws:ecs:${var.region}:${local.account_id}:service/${local.cluster_name}/*"]
  }
  statement {
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_iam_role_policy" "killswitch" {
  name   = "scale-to-zero"
  role   = aws_iam_role.killswitch.id
  policy = data.aws_iam_policy_document.killswitch.json
}

resource "aws_cloudwatch_log_group" "killswitch" {
  name              = "/aws/lambda/${local.project}-killswitch"
  retention_in_days = 30
}

resource "aws_lambda_function" "killswitch" {
  function_name    = "${local.project}-killswitch"
  description      = "Scales every service in ${local.cluster_name} to zero when the budget is exceeded."
  role             = aws_iam_role.killswitch.arn
  runtime          = "python3.13"
  handler          = "handler.handler"
  filename         = data.archive_file.killswitch.output_path
  source_code_hash = data.archive_file.killswitch.output_base64sha256
  timeout          = 60
  memory_size      = 128

  environment {
    variables = {
      CLUSTER_NAME    = local.cluster_name
      ALERT_TOPIC_ARN = aws_sns_topic.alerts.arn
    }
  }

  depends_on = [aws_cloudwatch_log_group.killswitch, aws_iam_role_policy_attachment.killswitch_logs]
}

resource "aws_sns_topic_subscription" "killswitch" {
  topic_arn = aws_sns_topic.killswitch.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.killswitch.arn
}

resource "aws_lambda_permission" "killswitch" {
  statement_id  = "AllowBudgetTopic"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.killswitch.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.killswitch.arn
}
