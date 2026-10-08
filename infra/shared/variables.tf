variable "region" {
  description = "AWS region of every CodeAtlas resource."
  type        = string
  default     = "us-east-1"
}

variable "domain" {
  description = "The domain delegated to the Route 53 hosted zone, for example example.dev. Each environment's hostname is under it."
  type        = string

  validation {
    condition     = can(regex("^([a-z0-9-]+\\.)+[a-z]{2,}$", var.domain))
    error_message = "domain must be a lowercase domain name such as example.dev."
  }
}

variable "github_oidc_subject_prefix" {
  description = "The repository's OIDC subject prefix, read with `gh api repos/<owner>/<repo>/actions/oidc/customization/sub`, for example repo:<owner>@<owner ID>/<repo>@<repo ID>. The release role trusts <prefix>:environment:pilot."
  type        = string

  validation {
    condition     = startswith(var.github_oidc_subject_prefix, "repo:")
    error_message = "github_oidc_subject_prefix must start with \"repo:\"."
  }
}

variable "alert_email" {
  description = "The address that receives alerts and budget notifications."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be an email address."
  }
}

variable "monthly_budget_usd" {
  description = "The monthly hosting budget in US dollars."
  type        = number
  default     = 40
}
