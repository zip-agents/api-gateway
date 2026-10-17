output "user_pool_id" {
  value = aws_cognito_user_pool.main.id
}

output "user_pool_arn" {
  value = aws_cognito_user_pool.main.arn
}

output "client_id" {
  value = aws_cognito_user_pool_client.gateway.id
}

output "client_secret" {
  value     = aws_cognito_user_pool_client.gateway.client_secret
  sensitive = true
}

output "domain" {
  description = "The hosted login's base URL (GATEWAY_COGNITO_DOMAIN)."
  value       = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${data.aws_region.current.region}.amazoncognito.com"
}
