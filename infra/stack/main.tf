# One environment on one host (specs/004-pilot-deployment research R1 to R3, R8, and R9): the
# instance and its role, the data volume, the Elastic IP, and the security group.

locals {
  region          = substr(var.availability_zone, 0, length(var.availability_zone) - 1)
  is_pilot        = var.environment == "pilot"
  name            = "codeatlas-${var.environment}"
  parameters_path = coalesce(var.parameters_path, "/codeatlas/${var.environment}")
  account_id      = data.aws_caller_identity.current.account_id
  partition       = data.aws_partition.current.partition
  # Created by infra/shared with these names.
  registry = "${local.account_id}.dkr.ecr.${local.region}.amazonaws.com"
  bucket   = "codeatlas-${local.account_id}-${local.region}"

  data_volume_id = local.is_pilot ? aws_ebs_volume.pilot_data[0].id : aws_ebs_volume.temporary_data[0].id
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnet" "host" {
  vpc_id            = data.aws_vpc.default.id
  availability_zone = var.availability_zone
  default_for_az    = true
}

# Canonical's current Ubuntu 24.04 image for arm64. The instance ignores later changes to it;
# replacing the host picks up the newest (research R2).
data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
}

data "aws_ecr_repository" "image" {
  for_each = toset(["codeatlas/api", "codeatlas/web"])

  name = each.key
}

data "aws_s3_bucket" "main" {
  bucket = local.bucket
}

# Only HTTP and HTTPS reach the host; administration goes through Session Manager (FR-007).
resource "aws_security_group" "host" {
  name        = "${local.name}-host"
  description = "CodeAtlas ${var.environment}: HTTP and HTTPS in, everything out"
  vpc_id      = data.aws_vpc.default.id

  tags = {
    Name = "${local.name}-host"
  }
}

resource "aws_vpc_security_group_ingress_rule" "web" {
  for_each = toset(["80", "443"])

  security_group_id = aws_security_group.host.id
  description       = "TCP ${each.key} from anywhere"
  ip_protocol       = "tcp"
  from_port         = tonumber(each.key)
  to_port           = tonumber(each.key)
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.host.id
  description       = "Everything out"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# The instance role. Only host scripts and the Docker daemon use it; the metadata hop limit keeps
# it out of the containers (research R2).
data "aws_iam_policy_document" "assume_ec2" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "host" {
  name               = "${local.name}-host"
  assume_role_policy = data.aws_iam_policy_document.assume_ec2.json
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.host.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "host" {
  statement {
    sid       = "RegistryLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "PullImages"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [for repository in data.aws_ecr_repository.image : repository.arn]
  }

  # GetParametersByPath is authorized against the path's own ARN as well as the names under it.
  statement {
    sid     = "ReadParameters"
    actions = ["ssm:GetParametersByPath"]
    resources = [
      "arn:${local.partition}:ssm:${local.region}:${local.account_id}:parameter${local.parameters_path}",
      "arn:${local.partition}:ssm:${local.region}:${local.account_id}:parameter${local.parameters_path}/*",
    ]
  }

  statement {
    sid       = "ReadReleaseBundles"
    actions   = ["s3:GetObject"]
    resources = ["${data.aws_s3_bucket.main.arn}/releases/*"]
  }

  # The Docker daemon's awslogs driver and Run Command's output.
  statement {
    sid = "WriteLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:DescribeLogStreams",
      "logs:PutLogEvents",
    ]
    resources = ["arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/codeatlas/${var.environment}/*"]
  }

  # Run Command checks that its output log group exists.
  statement {
    sid       = "FindLogGroups"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "host" {
  name   = "codeatlas-host"
  role   = aws_iam_role.host.id
  policy = data.aws_iam_policy_document.host.json
}

resource "aws_iam_instance_profile" "host" {
  name = "${local.name}-host"
  role = aws_iam_role.host.name
}

resource "aws_instance" "host" {
  ami                    = data.aws_ssm_parameter.ubuntu.insecure_value
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnet.host.id
  vpc_security_group_ids = [aws_security_group.host.id]
  iam_instance_profile   = aws_iam_instance_profile.host.name

  # Runs once per instance, so a change to it replaces the host (research R2).
  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    environment        = var.environment
    region             = local.region
    registry           = local.registry
    bucket             = local.bucket
    hostname           = var.hostname
    parameters_path    = local.parameters_path
    data_volume_device = "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${replace(local.data_volume_id, "-", "")}"
  })
  user_data_replace_on_change = true

  # IMDSv2 only, one hop: processes in containers cannot reach the role's credentials.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  # Throttle instead of billing CPU use above the baseline (research R1).
  credit_specification {
    cpu_credits = "standard"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_gib
    encrypted   = true
  }

  tags = {
    Name                    = local.name
    "codeatlas:environment" = var.environment
  }

  lifecycle {
    ignore_changes = [ami]
  }
}

# The data volume: PostgreSQL, Caddy's certificates, backup staging, and the release state. A
# lifecycle setting must be a literal, so the pilot's protected volume and the temporary
# environments' volume are separate resources (research R8).
resource "aws_ebs_volume" "pilot_data" {
  count = local.is_pilot ? 1 : 0

  availability_zone = var.availability_zone
  size              = var.data_volume_gib
  type              = "gp3"
  encrypted         = true

  tags = {
    Name                    = "${local.name}-data"
    "codeatlas:environment" = var.environment
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_ebs_volume" "temporary_data" {
  count = local.is_pilot ? 0 : 1

  availability_zone = var.availability_zone
  size              = var.data_volume_gib
  type              = "gp3"
  encrypted         = true

  tags = {
    Name                    = "${local.name}-data"
    "codeatlas:environment" = var.environment
  }
}

# Replacing the host stops the old instance before detaching, so the volume is never detached while
# mounted.
resource "aws_volume_attachment" "data" {
  device_name                    = "/dev/sdf"
  volume_id                      = local.data_volume_id
  instance_id                    = aws_instance.host.id
  stop_instance_before_detaching = true
}

# A stable address, kept when the host is replaced.
resource "aws_eip" "host" {
  domain = "vpc"

  tags = {
    Name = local.name
  }
}

resource "aws_eip_association" "host" {
  allocation_id = aws_eip.host.id
  instance_id   = aws_instance.host.id
}
