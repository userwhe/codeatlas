terraform {
  # 1.11 makes the S3 backend's native lock file generally available.
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # The state bucket comes from backend.hcl, and the key, stack/<environment>.tfstate, is given
  # per environment at init; see backend.hcl.example.
  backend "s3" {
    use_lockfile = true
  }
}

provider "aws" {
  region = local.region

  default_tags {
    tags = {
      project = "codeatlas"
    }
  }
}
