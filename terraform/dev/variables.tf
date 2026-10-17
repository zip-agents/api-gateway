variable "region" {
  type = string
}

variable "local_url" {
  description = "Where the local gateway is reached in the browser (GATEWAY_PUBLIC_URL)."
  type        = string
  default     = "http://localhost:8080"
}
