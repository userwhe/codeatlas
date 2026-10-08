# One log group per service, plus Run Command output (releases) and the host scripts' files
# (host), each kept 7 days (research R9).
resource "aws_cloudwatch_log_group" "codeatlas" {
  for_each = toset(["caddy", "web", "api", "worker", "db", "releases", "host"])

  name              = "/codeatlas/${var.environment}/${each.key}"
  retention_in_days = 7
}
