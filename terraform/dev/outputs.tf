# Read by scripts/dev-auth.sh, which writes them into .env for docker compose.

output "user_pool_id" {
  value = module.auth.user_pool_id
}

output "client_id" {
  value = module.auth.client_id
}

output "client_secret" {
  value     = module.auth.client_secret
  sensitive = true
}

output "domain" {
  value = module.auth.domain
}

output "public_url" {
  value = var.local_url
}

output "access_key_id" {
  value = aws_iam_access_key.gateway.id
}

output "secret_access_key" {
  value     = aws_iam_access_key.gateway.secret
  sensitive = true
}
