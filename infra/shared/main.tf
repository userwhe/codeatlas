# Resources shared by every environment (specs/004-pilot-deployment research R6, R8, R10, and
# R12): the image repositories, the bucket for release bundles and backups, the hosted zone, the
# alert topic, and the monthly budget.

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

# Release bundles under releases/ and nightly backups under backups/ (research R12).
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

  # Nightly backups (research R12). Without versioning, a backup is gone 7 days after it is taken,
  # rounded up to the next midnight UTC, which the deletion disclosure counts on (FR-018).
  rule {
    id     = "expire-backups"
    status = "Enabled"

    filter {
      prefix = "backups/"
    }

    expiration {
      days = 7
    }
  }
}

# Delegate the domain to this zone's name servers at the registrar (docs/operations.md).
resource "aws_route53_zone" "main" {
  name = var.domain
}

# Alarms (infra/stack, pilot only) and failed releases notify this topic, which emails the
# developer once the subscription is confirmed (research R10). The stacks look it up by name.
resource "aws_sns_topic" "alerts" {
  name = "codeatlas-alerts"
}

resource "aws_sns_topic_subscription" "alert_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# Emails once per threshold and month; forecasts need about five weeks of data (research R10).
resource "aws_budgets_budget" "monthly" {
  name         = "codeatlas-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = [
      { type = "ACTUAL", threshold = 80 },
      { type = "ACTUAL", threshold = 100 },
      { type = "FORECASTED", threshold = 80 },
    ]

    content {
      notification_type          = notification.value.type
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value.threshold
      threshold_type             = "PERCENTAGE"
      subscriber_email_addresses = [var.alert_email]
    }
  }
}
