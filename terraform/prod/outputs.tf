output "url" {
  value = "https://${var.domain_name}"
}

output "load_balancer_dns_name" {
  value = aws_lb.main.dns_name
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "gateway_secret_arn" {
  value = aws_secretsmanager_secret.gateway.arn
}

output "cognito_user_pool_id" {
  value = module.auth.user_pool_id
}
