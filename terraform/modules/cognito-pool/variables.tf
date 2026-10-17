variable "name" {
  description = "User pool name, e.g. zip-platform-prod."
  type        = string
}

variable "domain_prefix" {
  description = "Hosted login domain: https://<prefix>.auth.<region>.amazoncognito.com. Unique per region; may not contain aws, amazon or cognito."
  type        = string
}

variable "site_name" {
  description = "Named in the invitation email."
  type        = string
  default     = "zip gateway"
}

variable "login_url" {
  description = "The portal's sign-in page, linked from the invitation email."
  type        = string
}

variable "callback_urls" {
  description = "The gateway's /docs/auth/callback URL(s)."
  type        = list(string)
}

variable "logout_urls" {
  description = "Where Cognito may send the browser after signing out: the gateway's /docs/login."
  type        = list(string)
}

variable "temporary_password_days" {
  type    = number
  default = 7
}
