# Resources shared by every environment (specs/004-pilot-deployment research R6, R8, and R12):
# the image repositories, the bucket for release bundles and backups, and the hosted zone.

data "aws_caller_identity" "current" {}

locals {
  image_repositories = toset(["codeatlas/api", "codeatlas/web"])
  # infra/stack derives the same name, so the stacks need no remote state.
  bucket = "codeatlas-${data.aws_caller_identity.current.account_id}-${var.region}"
}

# One repository per image. Tags are full commit SHAs and never move (research R6).
resource "aws_ecr_repository" "image" {
  for_each = local.image_repositories

  name                 = each.key
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "image" {
  for_each = aws_ecr_repository.image

  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 20 newest images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}

# Release bundles under releases/ (and nightly backups, research R12).
resource "aws_s3_bucket" "main" {
  bucket = local.bucket
}

resource "aws_s3_bucket_public_access_block" "main" {
  bucket = aws_s3_bucket.main.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "main" {
  bucket = aws_s3_bucket.main.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# A bucket has exactly one lifecycle configuration, so every expiry rule belongs here.
resource "aws_s3_bucket_lifecycle_configuration" "main" {
  bucket = aws_s3_bucket.main.id

  rule {
    id     = "expire-releases"
    status = "Enabled"

    filter {
      prefix = "releases/"
    }

    expiration {
      days = 30
    }
  }
}

# Delegate the domain to this zone's name servers at the registrar (docs/operations.md).
resource "aws_route53_zone" "main" {
  name = var.domain
}
