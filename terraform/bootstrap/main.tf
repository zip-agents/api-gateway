# Account-level foundations, applied once from an administrator's machine
# (`make bootstrap`). Everything else is deployed by CI with the roles defined here.

data "aws_caller_identity" "current" {}

locals {
  project     = "zip-platform"
  environment = "prod"
  account_id  = data.aws_caller_identity.current.account_id
  # The ECS cluster the prod stack creates; the kill switch and deploy roles refer to it.
  cluster_name = "${local.project}-${local.environment}"
  services     = { for name, repo in var.github_repositories : name => repo if name != "platform" }
}

# ------------------------------------------------------------------------- Terraform state

resource "aws_s3_bucket" "state" {
  bucket = "${local.project}-tfstate-${local.account_id}-${var.region}"
  # `make nuke` deletes everything, state history included.
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

# ------------------------------------------------------------------------- image registry

# One repository per service. Tags are immutable (a tag always names the image that was
# tested), and force_delete lets teardown remove repositories that still hold images.
resource "aws_ecr_repository" "service" {
  for_each = local.services

  name                 = "${local.project}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "service" {
  for_each   = aws_ecr_repository.service
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 20 most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}
