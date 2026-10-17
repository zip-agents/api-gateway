terraform {
  required_version = "~> 1.16"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.60"
    }
  }

  # Local state on purpose: the dev pool is yours alone, does not depend on the bootstrap
  # stack, and its state (which holds the dev client secret and access key) stays on your
  # machine. terraform.tfstate is gitignored; `make aws-nuke` destroys this stack too.
  backend "local" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project     = "zip-platform"
      environment = "dev"
      stack       = "dev-auth"
      managed-by  = "terraform"
    }
  }
}
