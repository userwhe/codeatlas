output "ecr_repositories" {
  description = "Repository URL of each image, by repository name."
  value       = { for name, repository in aws_ecr_repository.image : name => repository.repository_url }
}

output "bucket" {
  description = "The bucket for release bundles and backups."
  value       = aws_s3_bucket.main.bucket
}

output "alert_topic_arn" {
  description = "The topic that alarms and failed releases notify."
  value       = aws_sns_topic.alerts.arn
}

output "zone_id" {
  description = "The hosted zone of the domain."
  value       = aws_route53_zone.main.zone_id
}

output "name_servers" {
  description = "The name servers to set at the domain's registrar."
  value       = aws_route53_zone.main.name_servers
}
