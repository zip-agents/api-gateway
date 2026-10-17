# The shared platform the services deploy onto: HTTPS load balancer, ECS cluster, private
# DNS for service discovery, the gateway's secret, and SSM parameters that hand all of it
# to the service repositories. Each service's own resources are in its repository
# (deploy/, using ../modules/ecs-service).

locals {
  project     = "zip-platform"
  environment = "prod"
  name        = "${local.project}-${local.environment}"
}

# ------------------------------------------------------------------------- HTTPS and DNS

data "aws_route53_zone" "main" {
  name         = var.hosted_zone_name
  private_zone = false
}

resource "aws_acm_certificate" "main" {
  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "certificate_validation" {
  for_each = {
    for option in aws_acm_certificate.main.domain_validation_options :
    option.domain_name => option
  }
  zone_id         = data.aws_route53_zone.main.zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "main" {
  certificate_arn         = aws_acm_certificate.main.arn
  validation_record_fqdns = [for record in aws_route53_record.certificate_validation : record.fqdn]
}

resource "aws_route53_record" "gateway" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = var.domain_name
  type    = "A"
  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = false
  }
}

# ------------------------------------------------------------------------- load balancer

resource "aws_lb" "main" {
  name                       = local.name
  load_balancer_type         = "application"
  internal                   = false
  subnets                    = aws_subnet.public[*].id
  security_groups            = [aws_security_group.alb.id]
  idle_timeout               = var.alb_idle_timeout_seconds
  drop_invalid_header_fields = true
  enable_deletion_protection = false # teardown must always succeed
}

resource "aws_lb_target_group" "gateway" {
  name        = "${local.name}-gateway"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"
  protocol    = "HTTP"
  port        = 8000
  # Streams in flight get this long to finish when a gateway task is replaced.
  deregistration_delay = 120

  # Both halves of a browser upload job (download and upload) must reach the same gateway
  # task, which holds the job in memory. Harmless with one task; required with more.
  stickiness {
    type            = "lb_cookie"
    enabled         = true
    cookie_duration = 86400
  }

  health_check {
    path                = "/health/ready"
    matcher             = "200"
    interval            = 10
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.main.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gateway.arn
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      protocol    = "HTTPS"
      port        = "443"
      status_code = "HTTP_301"
    }
  }
}

# ------------------------------------------------------------------------- ECS

resource "aws_ecs_cluster" "main" {
  name = local.name

  # Container Insights costs extra per metric; the services' JSON logs are enough here.
  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]
}

# Plain DNS (A records), not ECS Service Connect: Service Connect's Envoy proxy times
# requests out after 15 s by default, which would cut long streams.
resource "aws_service_discovery_private_dns_namespace" "main" {
  name        = "zip.internal"
  description = "Zip services, resolvable from the gateway as <name>.zip.internal"
  vpc         = aws_vpc.main.id
}

# ------------------------------------------------------------------------- gateway secret

# Values are written by `make api-keys` (scripts/api-keys.sh), never by Terraform, so they
# never appear in state. The gateway refuses to start until GATEWAY_API_KEYS is set.
resource "aws_secretsmanager_secret" "gateway" {
  name                    = "${local.project}/${local.environment}/gateway"
  description             = "GATEWAY_API_KEYS (name:secret,...) and GATEWAY_SESSION_SECRET."
  recovery_window_in_days = 0 # teardown deletes it at once
}

# ------------------------------------------------------------------------- hand-over to services

locals {
  parameters = {
    "cluster_name"               = aws_ecs_cluster.main.name
    "cluster_arn"                = aws_ecs_cluster.main.arn
    "private_subnet_ids"         = join(",", aws_subnet.private[*].id)
    "gateway_security_group_id"  = aws_security_group.gateway.id
    "services_security_group_id" = aws_security_group.services.id
    "namespace_id"               = aws_service_discovery_private_dns_namespace.main.id
    "namespace_name"             = aws_service_discovery_private_dns_namespace.main.name
    "gateway_target_group_arn"   = aws_lb_target_group.gateway.arn
    "gateway_secret_arn"         = aws_secretsmanager_secret.gateway.arn
    "domain_name"                = var.domain_name
    "cognito_user_pool_id"       = module.auth.user_pool_id
    "cognito_user_pool_arn"      = module.auth.user_pool_arn
    "cognito_client_id"          = module.auth.client_id
    "cognito_domain"             = module.auth.domain
    "cognito_secret_arn"         = aws_secretsmanager_secret.cognito.arn
    "database_host"              = aws_rds_cluster.accounts.endpoint
    "database_port"              = tostring(aws_rds_cluster.accounts.port)
    "database_name"              = local.database_name
    "database_user"              = local.database_user
    "database_secret_arn"        = aws_secretsmanager_secret.database.arn
  }
}

# Service repositories read these instead of this stack's state: the only contract
# between the platform and a service is this list of names.
resource "aws_ssm_parameter" "platform" {
  for_each    = local.parameters
  name        = "/${local.project}/${local.environment}/${each.key}"
  type        = "String"
  value       = each.value
  description = "Platform output for service deployments (terraform/prod)."
}
