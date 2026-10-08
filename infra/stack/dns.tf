# The environment's name in the shared hosted zone (infra/shared).
data "aws_route53_zone" "main" {
  name         = var.domain
  private_zone = false
}

resource "aws_route53_record" "host" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = var.hostname
  type    = "A"
  ttl     = 300
  records = [aws_eip.host.public_ip]
}
