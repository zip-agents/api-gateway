terraform {
  required_version = "~> 1.16"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.60"
    }
  }

  # Bucket and region come from -backend-config (CI, or by hand: docs/aws-runbook.md).
  backend "s3" {
    key          = "services/gateway/prod.tfstate"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project     = "zip-platform"
      environment = "prod"
      service     = "gateway"
      managed-by  = "terraform"
    }
  }
}
