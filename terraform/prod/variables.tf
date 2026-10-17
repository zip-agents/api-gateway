variable "region" {
  type = string
}

variable "hosted_zone_name" {
  description = "The Route 53 zone created when the domain was registered, e.g. example.com. Looked up, never managed."
  type        = string
}

variable "domain_name" {
  description = "Public hostname of the gateway, inside the hosted zone: e.g. zip.example.com, or the apex."
  type        = string
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "alb_idle_timeout_seconds" {
  description = "Must exceed the gateway's 60 s and the services' 30 s idle limits; streams move bytes continuously."
  type        = number
  default     = 120
}
