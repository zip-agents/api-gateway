# A Cognito user pool for the gateway's portal: invite-only accounts (no self sign-up),
# email as the username, Cognito's hosted login page, and a confidential app client for
# the gateway (authorization code + PKCE, client secret). Used by terraform/prod; see
# gateway/gateway/accounts/ for the other side, and fake-cognito/ for the local stand-in.

data "aws_region" "current" {}

resource "aws_cognito_user_pool" "main" {
  name = var.name
  # Lite covers everything used here; 10,000 monthly active users are free.
  user_pool_tier      = "LITE"
  deletion_protection = "INACTIVE" # teardown must always succeed

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  username_configuration {
    case_sensitive = false
  }

  # Accounts are created only by the gateway, when someone redeems an invite code (no
  # email: they choose their own password), or by `make aws-admin`, for which Cognito
  # emails this temporary password.
  admin_create_user_config {
    allow_admin_create_user_only = true
    invite_message_template {
      email_subject = "Your ${var.site_name} account"
      email_message = <<-EOT
        Your ${var.site_name} account is ready.<br><br>
        Sign in at <a href="${var.login_url}">${var.login_url}</a> with<br>
        email: {username}<br>
        temporary password: {####}<br><br>
        You will choose your own password when you first sign in. The temporary password
        expires in ${var.temporary_password_days} days.
      EOT
      sms_message   = "Your ${var.site_name} username is {username} and temporary password is {####}."
    }
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = var.temporary_password_days
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # Users may add an authenticator app; nobody is forced to.
  mfa_configuration = "OPTIONAL"
  software_token_mfa_configuration {
    enabled = true
  }

  # Cognito's own sender: about 50 emails a day, enough for invites. SES can replace it.
  email_configuration {
    email_sending_account = "COGNITO_DEFAULT"
  }

  # Which invite code created the account; set by the gateway, shown on its admin pages.
  schema {
    name                     = "invite_id"
    attribute_data_type      = "String"
    mutable                  = true
    developer_only_attribute = false
    string_attribute_constraints {
      min_length = 0
      max_length = 32
    }
  }
}

resource "aws_cognito_user_group" "admin" {
  name         = "admin"
  user_pool_id = aws_cognito_user_pool.main.id
  description  = "Can create invite codes and see users, sign-ins and API keys in the portal."
}

resource "aws_cognito_user_pool_domain" "main" {
  domain                = var.domain_prefix
  user_pool_id          = aws_cognito_user_pool.main.id
  managed_login_version = 1 # the classic hosted UI: no branding resources needed
}

resource "aws_cognito_user_pool_client" "gateway" {
  name         = "gateway"
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = var.callback_urls
  logout_urls                          = var.logout_urls
  explicit_auth_flows                  = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true

  # The gateway only reads the ID token once, at sign-in; its own session follows.
  id_token_validity     = 5
  access_token_validity = 5
  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "days"
  }

  read_attributes = ["email", "email_verified", "custom:invite_id"]
  # Users must not be able to rewrite which invite created them.
  write_attributes = ["email"]
}
