terraform {
  required_version = "~> 1.16"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.60"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.8"
    }
  }

  # The bucket is created by this stack, so the first apply runs with local state
  # (docs/aws-runbook.md adds a local_override.tf) and then migrates into it. The bucket
  # name and region are passed with -backend-config.
  backend "s3" {
    key          = "bootstrap.tfstate"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project    = local.project
      stack      = "bootstrap"
      managed-by = "terraform"
    }
  }
}
