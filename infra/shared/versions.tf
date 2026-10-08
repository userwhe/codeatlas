terraform {
  # 1.11 makes the S3 backend's native lock file generally available.
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # The state bucket and key come from `terraform init -backend-config=backend.hcl`; see
  # backend.hcl.example.
  backend "s3" {
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project = "codeatlas"
    }
  }
}
