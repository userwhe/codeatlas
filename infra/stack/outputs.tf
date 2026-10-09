output "instance_id" {
  description = "The host, for Session Manager and Run Command."
  value       = aws_instance.host.id
}

output "public_ip" {
  description = "The host's Elastic IP address."
  value       = aws_eip.host.public_ip
}

output "url" {
  description = "The environment's address."
  value       = "https://${var.hostname}"
}
