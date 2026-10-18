terraform {
  required_version = "~> 1.16"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.60"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  # Bucket and region come from -backend-config (CI, or by hand: docs/aws-runbook.md).
  backend "s3" {
    key          = "platform/prod.tfstate"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project     = local.project
      environment = local.environment
      stack       = "platform"
      managed-by  = "terraform"
    }
  }
}
