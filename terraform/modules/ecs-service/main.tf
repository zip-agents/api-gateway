# One ECS Fargate service: task definition, execution role, log group, the service itself,
# and either Cloud Map registration (zip services) or a load balancer target (the gateway).
#
# Called from each service repository's deploy/ directory, pinned to a tag of the
# platform repository. Deliberately small: anything a service needs beyond this is a sign
# the shared platform should grow, not that the module should take more options.

data "aws_region" "current" {}

locals {
  full_name = "${var.project}-${var.environment}-${var.name}"
  log_group = "/${var.project}/${var.environment}/${var.name}"
}

resource "aws_cloudwatch_log_group" "service" {
  name              = local.log_group
  retention_in_days = var.log_retention_days
}

# ------------------------------------------------------------------------- execution role
# Used by ECS to pull the image, write logs and read secrets. The task role (below) is
# separate: it is what the running code may call, and only exists when a service needs it.

data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.full_name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

data "aws_iam_policy_document" "execution" {
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = [var.ecr_repository_arn]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.service.arn}:*"]
  }
  dynamic "statement" {
    for_each = length(var.secret_arns) > 0 ? [1] : []
    content {
      actions   = ["secretsmanager:GetSecretValue"]
      resources = var.secret_arns
    }
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "execution"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# ------------------------------------------------------------------------- task role
# Only for services whose code calls AWS (the gateway: Cognito user administration).

resource "aws_iam_role" "task" {
  count              = var.task_role_policy_json == null ? 0 : 1
  name               = "${local.full_name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy" "task" {
  count  = var.task_role_policy_json == null ? 0 : 1
  name   = "task"
  role   = aws_iam_role.task[0].id
  policy = var.task_role_policy_json
}

# ------------------------------------------------------------------------- task definition

resource "aws_ecs_task_definition" "service" {
  family                   = local.full_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = one(aws_iam_role.task[*].arn)

  runtime_platform {
    cpu_architecture        = "X86_64" # the architecture CI builds and tests
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([{
    name                   = var.name
    image                  = var.image
    essential              = true
    readonlyRootFilesystem = true # contract §11: the images run with a read-only root
    stopTimeout            = var.stop_timeout_seconds
    portMappings           = [{ containerPort = var.container_port, protocol = "tcp" }]
    environment = [
      for key in sort(keys(var.environment_variables)) :
      { name = key, value = var.environment_variables[key] }
    ]
    secrets = [
      for key in sort(keys(var.secrets)) :
      { name = key, valueFrom = var.secrets[key] }
    ]
    healthCheck = {
      command = [
        "CMD", "python", "-c",
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${var.container_port}${var.health_check_path}', timeout=2)",
      ]
      interval    = 10
      timeout     = 3
      retries     = 3
      startPeriod = 10
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.service.name
        awslogs-region        = data.aws_region.current.region
        awslogs-stream-prefix = var.name
      }
    }
  }])
}

# ------------------------------------------------------------------------- discovery

resource "aws_service_discovery_service" "service" {
  count = var.cloud_map_namespace_id == null ? 0 : 1
  name  = var.name

  dns_config {
    namespace_id   = var.cloud_map_namespace_id
    routing_policy = "MULTIVALUE"
    dns_records {
      type = "A"
      ttl  = 10
    }
  }

  # Registration follows the ECS task health check.
  health_check_custom_config {}
}

# ------------------------------------------------------------------------- service

resource "aws_ecs_service" "service" {
  name            = var.name
  cluster         = var.cluster_arn
  task_definition = aws_ecs_task_definition.service.arn
  desired_count   = var.desired_count

  capacity_provider_strategy {
    capacity_provider = var.capacity == "spot" ? "FARGATE_SPOT" : "FARGATE"
    weight            = 1
  }

  # Rolling deploys keep the old task until the new one is healthy; the circuit breaker
  # rolls back a deploy whose tasks never become healthy.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = var.security_group_ids
    assign_public_ip = false
  }

  dynamic "service_registries" {
    for_each = aws_service_discovery_service.service
    content {
      registry_arn = service_registries.value.arn
    }
  }

  dynamic "load_balancer" {
    for_each = var.target_group_arn == null ? [] : [var.target_group_arn]
    content {
      target_group_arn = load_balancer.value
      container_name   = var.name
      container_port   = var.container_port
    }
  }

  health_check_grace_period_seconds = var.target_group_arn == null ? null : 30
  propagate_tags                    = "SERVICE"
  enable_ecs_managed_tags           = true
  wait_for_steady_state             = true # `terraform apply` fails if the deploy does

  tags = {
    service = var.name
    # Read by `make resume` to restore the running count after a pause or the kill switch.
    "zip-platform:desired-count" = tostring(var.desired_count)
  }

  lifecycle {
    # The running count belongs to pause/resume and the kill switch once the service
    # exists; a deploy must never undo a pause.
    ignore_changes = [desired_count]
  }
}
