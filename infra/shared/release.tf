# Credentials for the release workflow (specs/004-pilot-deployment research R7; ADR 0011): GitHub's
# OIDC provider and the only release role. No AWS key is stored in GitHub (FR-024).

data "aws_partition" "current" {}

# An account has at most one provider per URL; if it already has GitHub's, import it instead
# (docs/operations.md, "Setting up releases").
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

# Trusted only for jobs of this repository that run in the `pilot` environment, which requires the
# developer's approval and allows only `main`. The subject uses GitHub's immutable format, so the
# prefix (with the owner and repository IDs) is a variable.
data "aws_iam_policy_document" "release_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${var.github_oidc_subject_prefix}:environment:pilot"]
    }
  }
}

resource "aws_iam_role" "release" {
  name               = "codeatlas-release"
  description        = "The release workflow's job in the pilot environment, after approval"
  assume_role_policy = data.aws_iam_policy_document.release_trust.json
}

data "aws_iam_policy_document" "release" {
  statement {
    sid       = "RegistryLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # Push both images, and find out whether a tag already exists (tags are immutable).
  statement {
    sid = "PushImages"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [for repository in aws_ecr_repository.image : repository.arn]
  }

  statement {
    sid       = "UploadReleaseBundles"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.main.arn}/releases/*"]
  }

  # Run Command authorizes SendCommand against each target instance and against the document, so
  # the two need separate statements: the instances only when tagged as the pilot.
  statement {
    sid       = "RunCommandOnThePilot"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:${data.aws_partition.current.partition}:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/*"]

    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/codeatlas:environment"
      values   = ["pilot"]
    }
  }

  statement {
    sid       = "RunShellScriptDocument"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:${data.aws_partition.current.partition}:ssm:${var.region}::document/AWS-RunShellScript"]
  }

  # The command's status and exit status, while the workflow waits for it to end.
  statement {
    sid = "ReadCommandResults"
    actions = [
      "ssm:GetCommandInvocation",
      "ssm:ListCommandInvocations",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "AlertOnFailure"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_iam_role_policy" "release" {
  name   = "codeatlas-release"
  role   = aws_iam_role.release.id
  policy = data.aws_iam_policy_document.release.json
}
