# The pilot's monitoring (specs/004-pilot-deployment research R10; contracts/operations.md,
# "Health checks and alarms"): two Route 53 health checks, ten alarms that notify the shared
# codeatlas-alerts topic when they fire and when they recover, the dashboard codeatlas-pilot, and
# saved Logs Insights queries. The drill and the load test have none of them.

locals {
  health_check_paths = local.is_pilot ? { readyz = "/readyz", web = "/" } : {}

  log_group_prefix = "/codeatlas/${var.environment}"

  # Route groups of the API's request lines: searches, views, submissions, and other (sign-in,
  # webhooks, health, deletions, and the job event stream, whose requests stay open).
  route_group = join(" ", [
    "case(route = \"/v1/search\", \"searches\",",
    "method = \"POST\" and startsWith(route, \"/v1/\") = 1, \"submissions\",",
    "method = \"GET\" and startsWith(route, \"/v1/\") = 1 and route != \"/v1/jobs/{job_id}/events\", \"views\",",
    "\"other\")",
  ])

  # Every alarm has the same attributes, so that they form one map. Daily metrics are evaluated
  # over 26 one-hour periods that must all breach, with missing data breaching: a longer single
  # period would let the previous day's data point keep the alarm quiet (research R10).
  alarms = local.is_pilot ? tomap({
    site-down = {
      description = "The API or its database is down: the /readyz health check has failed for 5 minutes."
      namespace   = "AWS/Route53"
      metric_name = "HealthCheckStatus"
      dimensions  = tomap({ HealthCheckId = aws_route53_health_check.pilot["readyz"].id })
      statistic   = "Minimum"
      period      = 60
      periods     = 5
      comparison  = "LessThanThreshold"
      threshold   = 1
      missing     = "missing"
    }
    web-down = {
      description = "The web app is down: the / health check has failed for 5 minutes."
      namespace   = "AWS/Route53"
      metric_name = "HealthCheckStatus"
      dimensions  = tomap({ HealthCheckId = aws_route53_health_check.pilot["web"].id })
      statistic   = "Minimum"
      period      = 60
      periods     = 5
      comparison  = "LessThanThreshold"
      threshold   = 1
      missing     = "missing"
    }
    worker-down = {
      description = "The worker has reported no heartbeat for 5 minutes."
      namespace   = "CodeAtlas"
      metric_name = "WorkerHeartbeat"
      dimensions  = tomap({ Environment = var.environment })
      statistic   = "Sum"
      period      = 60
      periods     = 5
      comparison  = "LessThanThreshold"
      threshold   = 1
      missing     = "breaching"
    }
    jobs-waiting = {
      description = "A job that could run has waited 30 minutes or more: the worker is falling behind."
      namespace   = "CodeAtlas"
      metric_name = "OldestRunnableJobAgeSeconds"
      dimensions  = tomap({ Environment = var.environment })
      statistic   = "Maximum"
      period      = 300
      periods     = 1
      comparison  = "GreaterThanOrEqualToThreshold"
      threshold   = 1800
      missing     = "missing"
    }
    jobs-failing = {
      description = "3 or more jobs failed within an hour (retries used up, timeouts, or internal errors)."
      namespace   = "CodeAtlas"
      metric_name = "JobsFailed"
      dimensions  = tomap({ Environment = var.environment })
      statistic   = "Sum"
      period      = 3600
      periods     = 1
      comparison  = "GreaterThanOrEqualToThreshold"
      threshold   = 3
      missing     = "notBreaching"
    }
    root-disk-full = {
      description = "The root volume is more than 80% full."
      namespace   = "CWAgent"
      metric_name = "disk_used_percent"
      dimensions  = tomap({ InstanceId = aws_instance.host.id, path = "/", fstype = "ext4" })
      statistic   = "Maximum"
      period      = 300
      periods     = 1
      comparison  = "GreaterThanThreshold"
      threshold   = 80
      missing     = "missing"
    }
    data-disk-full = {
      description = "The data volume (database, certificates, backup staging) is more than 80% full."
      namespace   = "CWAgent"
      metric_name = "disk_used_percent"
      dimensions  = tomap({ InstanceId = aws_instance.host.id, path = "/var/lib/codeatlas", fstype = "ext4" })
      statistic   = "Maximum"
      period      = 300
      periods     = 1
      comparison  = "GreaterThanThreshold"
      threshold   = 80
      missing     = "missing"
    }
    backup-missing = {
      description = "No backup has completed for 26 hours."
      namespace   = "CodeAtlas"
      metric_name = "BackupCompleted"
      dimensions  = tomap({ Environment = var.environment })
      statistic   = "Sum"
      period      = 3600
      periods     = 26
      comparison  = "LessThanThreshold"
      threshold   = 1
      missing     = "breaching"
    }
    certificate-expiring = {
      description = "The TLS certificate expires within 14 days, or has not been checked for 26 hours."
      namespace   = "CodeAtlas"
      metric_name = "CertificateDaysLeft"
      dimensions  = tomap({ Environment = var.environment })
      statistic   = "Minimum"
      period      = 3600
      periods     = 26
      comparison  = "LessThanThreshold"
      threshold   = 14
      missing     = "breaching"
    }
    host-status-check = {
      description = "The host failed an EC2 status check for 2 minutes."
      namespace   = "AWS/EC2"
      metric_name = "StatusCheckFailed"
      dimensions  = tomap({ InstanceId = aws_instance.host.id })
      statistic   = "Maximum"
      period      = 60
      periods     = 2
      comparison  = "GreaterThanOrEqualToThreshold"
      threshold   = 1
      missing     = "missing"
    }
  }) : {}
}

# HTTPS checks from several Route 53 regions (research R10).
resource "aws_route53_health_check" "pilot" {
  for_each = local.health_check_paths

  type              = "HTTPS"
  fqdn              = var.hostname
  port              = 443
  resource_path     = each.value
  enable_sni        = true
  request_interval  = 30
  failure_threshold = 3

  tags = {
    Name = "codeatlas-${var.environment}-${each.key}"
  }

  lifecycle {
    precondition {
      condition     = local.region == "us-east-1"
      error_message = "Route 53 publishes health check metrics only in us-east-1, and an alarm can notify only a topic in its own region, so the pilot must run in us-east-1."
    }
  }
}

# Created by infra/shared.
data "aws_sns_topic" "alerts" {
  count = local.is_pilot ? 1 : 0

  name = "codeatlas-alerts"
}

resource "aws_cloudwatch_metric_alarm" "pilot" {
  for_each = local.alarms

  alarm_name          = each.key
  alarm_description   = "CodeAtlas ${var.environment}: ${each.value.description}"
  namespace           = each.value.namespace
  metric_name         = each.value.metric_name
  dimensions          = each.value.dimensions
  statistic           = each.value.statistic
  period              = each.value.period
  evaluation_periods  = each.value.periods
  datapoints_to_alarm = each.value.periods
  comparison_operator = each.value.comparison
  threshold           = each.value.threshold
  treat_missing_data  = each.value.missing

  # A notice when it fires and another when it recovers (FR-014).
  alarm_actions = [data.aws_sns_topic.alerts[0].arn]
  ok_actions    = [data.aws_sns_topic.alerts[0].arn]
}

resource "aws_cloudwatch_dashboard" "pilot" {
  count = local.is_pilot ? 1 : 0

  dashboard_name = "codeatlas-${var.environment}"
  dashboard_body = jsonencode({
    start          = "-P7D"
    periodOverride = "inherit"
    widgets = [
      {
        type   = "alarm"
        x      = 0
        y      = 0
        width  = 24
        height = 3
        properties = {
          title  = "Alarms"
          alarms = [for alarm in aws_cloudwatch_metric_alarm.pilot : alarm.arn]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 3
        width  = 12
        height = 6
        properties = {
          title  = "Health checks (1 = healthy)"
          region = local.region
          view   = "timeSeries"
          stat   = "Minimum"
          period = 60
          metrics = [
            ["AWS/Route53", "HealthCheckStatus", "HealthCheckId", aws_route53_health_check.pilot["readyz"].id, { label = "/readyz" }],
            ["AWS/Route53", "HealthCheckStatus", "HealthCheckId", aws_route53_health_check.pilot["web"].id, { label = "/" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 3
        width  = 12
        height = 6
        properties = {
          title  = "Worker and queue"
          region = local.region
          view   = "timeSeries"
          period = 60
          metrics = [
            ["CodeAtlas", "WorkerHeartbeat", "Environment", var.environment, { stat = "Sum", label = "Heartbeats" }],
            ["CodeAtlas", "QueuedJobs", "Environment", var.environment, { stat = "Maximum", label = "Queued jobs" }],
            ["CodeAtlas", "OldestRunnableJobAgeSeconds", "Environment", var.environment, { stat = "Maximum", label = "Oldest runnable job (seconds)", yAxis = "right" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 9
        width  = 12
        height = 6
        properties = {
          title  = "Host CPU, memory, and disks (%)"
          region = local.region
          view   = "timeSeries"
          period = 300
          yAxis  = { left = { min = 0, max = 100 } }
          metrics = [
            ["AWS/EC2", "CPUUtilization", "InstanceId", aws_instance.host.id, { stat = "Average", label = "CPU" }],
            ["CWAgent", "mem_used_percent", "InstanceId", aws_instance.host.id, { stat = "Maximum", label = "Memory" }],
            ["CWAgent", "disk_used_percent", "InstanceId", aws_instance.host.id, "path", "/", "fstype", "ext4", { stat = "Maximum", label = "Root volume" }],
            ["CWAgent", "disk_used_percent", "InstanceId", aws_instance.host.id, "path", "/var/lib/codeatlas", "fstype", "ext4", { stat = "Maximum", label = "Data volume" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 9
        width  = 12
        height = 6
        properties = {
          title  = "Failed jobs, backups, and certificate"
          region = local.region
          view   = "timeSeries"
          period = 3600
          metrics = [
            ["CodeAtlas", "JobsFailed", "Environment", var.environment, { stat = "Sum", label = "Failed jobs per hour" }],
            ["CodeAtlas", "BackupCompleted", "Environment", var.environment, { stat = "Sum", label = "Backups" }],
            ["AWS/EC2", "StatusCheckFailed", "InstanceId", aws_instance.host.id, { stat = "Maximum", label = "Status check failed" }],
            ["CodeAtlas", "CertificateDaysLeft", "Environment", var.environment, { stat = "Minimum", label = "Certificate days left", yAxis = "right" }],
          ]
        }
      },
      {
        type   = "log"
        x      = 0
        y      = 15
        width  = 12
        height = 6
        properties = {
          title  = "Requests and p95 latency by route group"
          region = local.region
          view   = "table"
          query = join("\n| ", [
            "SOURCE '${local.log_group_prefix}/api' | filter message = \"request\"",
            "fields ${local.route_group} as route_group",
            "stats count(*) as requests, pct(duration_ms, 95) as p95_ms by route_group",
            "sort requests desc",
          ])
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 15
        width  = 12
        height = 6
        properties = {
          title  = "5xx responses by route group"
          region = local.region
          view   = "table"
          query = join("\n| ", [
            "SOURCE '${local.log_group_prefix}/api' | filter message = \"request\" and status >= 500",
            "fields ${local.route_group} as route_group",
            "stats count(*) as responses_5xx by route_group, route",
            "sort responses_5xx desc",
          ])
        }
      },
      {
        type   = "log"
        x      = 0
        y      = 21
        width  = 12
        height = 6
        properties = {
          title  = "Jobs by kind and outcome (retry_wait is a retry)"
          region = local.region
          view   = "table"
          query = join("\n| ", [
            "SOURCE '${local.log_group_prefix}/worker' | SOURCE '${local.log_group_prefix}/api' | filter message = \"job_finished\"",
            "stats count(*) as jobs, avg(duration_ms) as avg_ms, pct(duration_ms, 95) as p95_ms by kind, outcome",
            "sort jobs desc",
          ])
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 21
        width  = 12
        height = 6
        properties = {
          title  = "Model provider errors by provider"
          region = local.region
          view   = "table"
          query = join("\n| ", [
            "SOURCE '${local.log_group_prefix}/worker' | SOURCE '${local.log_group_prefix}/api' | filter ispresent(provider)",
            "stats count(*) as errors by provider, level",
            "sort errors desc",
          ])
        }
      },
    ]
  })
}

# Saved in Logs Insights under codeatlas-pilot/ (docs/operations.md, "Logs").
resource "aws_cloudwatch_query_definition" "lines_for_an_id" {
  count = local.is_pilot ? 1 : 0

  name            = "codeatlas-${var.environment}/Lines for a request or job ID"
  log_group_names = [for group in aws_cloudwatch_log_group.codeatlas : group.name]
  query_string    = <<-EOT
    # Replace REQUEST_OR_JOB_ID with the X-Request-ID of a response, or a job ID.
    fields @timestamp, @log, level, message, status, outcome
    | filter @message like "REQUEST_OR_JOB_ID"
    | sort @timestamp asc
    | limit 1000
  EOT
}

resource "aws_cloudwatch_query_definition" "p95_by_route" {
  count = local.is_pilot ? 1 : 0

  name            = "codeatlas-${var.environment}/p95 latency by route"
  log_group_names = [aws_cloudwatch_log_group.codeatlas["api"].name]
  query_string    = <<-EOT
    filter message = "request"
    | stats count(*) as requests, pct(duration_ms, 95) as p95_ms by method, route
    | sort p95_ms desc
  EOT
}

resource "aws_cloudwatch_query_definition" "failed_jobs" {
  count = local.is_pilot ? 1 : 0

  name = "codeatlas-${var.environment}/Failed jobs"
  log_group_names = [
    aws_cloudwatch_log_group.codeatlas["worker"].name,
    aws_cloudwatch_log_group.codeatlas["api"].name,
  ]
  query_string = <<-EOT
    filter message = "job_finished" and outcome = "failed"
    | fields @timestamp, kind, attempt, job_id
    | sort @timestamp desc
    | limit 200
  EOT
}
