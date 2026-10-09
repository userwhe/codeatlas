variable "environment" {
  description = "The environment this stack builds: pilot (always on), drill (a recovery exercise), or loadtest (a load test)."
  type        = string

  validation {
    condition     = contains(["pilot", "drill", "loadtest"], var.environment)
    error_message = "environment must be pilot, drill, or loadtest."
  }
}

variable "domain" {
  description = "The domain of the shared hosted zone (infra/shared), for example example.dev."
  type        = string
}

variable "hostname" {
  description = "The environment's public name, under domain, for example codeatlas.example.dev."
  type        = string

  validation {
    condition     = endswith(var.hostname, ".${var.domain}")
    error_message = "hostname must be a name under domain."
  }
}

variable "availability_zone" {
  description = "The zone of the host and its data volume; the region is derived from it."
  type        = string
  default     = "us-east-1a"
}

variable "instance_type" {
  description = "The host's instance type. It must be an Arm (Graviton) type: the AMI and the bootstrap are for arm64."
  type        = string
  default     = "t4g.medium"
}

variable "root_volume_gib" {
  description = "Size of the root volume (operating system, images, swap) in GiB."
  type        = number
  default     = 20
}

variable "data_volume_gib" {
  description = "Size of the data volume (PostgreSQL, certificates, backup staging, release state) in GiB."
  type        = number
  default     = 20
}

variable "parameters_path" {
  description = "The SSM parameter path that render-config.sh reads. Defaults to /codeatlas/<environment>; the drill reads the pilot's."
  type        = string
  default     = null

  validation {
    condition     = var.parameters_path == null || can(regex("^/codeatlas/[a-z]+$", var.parameters_path))
    error_message = "parameters_path must look like /codeatlas/<environment>."
  }
}
