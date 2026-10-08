# ADR 0010: The pilot runs on one EC2 host with Docker Compose

- **Status**: Accepted
- **Date**: 2026-10-07
- **Scope**: `specs/004-pilot-deployment`

## Context

The pilot serves at most 10 invited users. Its services are the web app, the API, the worker, and
PostgreSQL with pgvector (ADR 0001, ADR 0002); they already run together in Docker Compose. The
design document proposes ECS on Fargate, RDS, SQS, S3, and an HTTPS load balancer, and allows a
lower-cost profile on one EC2 instance. The spec caps hosting at $40 a month, excluding model
providers.

## Options considered

1. ECS on Fargate with RDS and an Application Load Balancer.
2. One EC2 instance running the Compose services behind Caddy.
3. Lightsail.
4. A platform service such as Fly.io, Render, or Railway.

## Decision

Option 2:

- One `t4g.medium` (Arm, 2 vCPUs, 4 GiB) in `us-east-1`, Ubuntu 24.04, with an Elastic IP, in the
  default VPC.
- Caddy terminates TLS with Let's Encrypt and routes API paths to the API and the rest to the web
  app. Only ports 80 and 443 are open; administration uses SSM Session Manager.
- PostgreSQL runs in a container, with its data on a separate encrypted EBS volume that survives
  host replacement, and a nightly `pg_dump` to S3 kept for 7 days that the host cannot overwrite or
  delete. The instance uses standard CPU credits, so load is throttled rather than billed.
- Images are built for Arm in GitHub Actions and stored in ECR. Credentials live in SSM Parameter
  Store. Containers get no AWS credentials (IMDSv2 hop limit 1).
- ADR 0002 (PostgreSQL as the only data store) and ADR 0003 (PostgreSQL job queue) stay as they
  are; S3 holds only backups and release bundles.

## Trade-off

- **Gained**:
  - About $34 a month instead of about $80; the fixed fees of a load balancer, RDS, and per-task
    public addresses disappear.
  - The deployed topology is the one tested in Compose and CI.
  - Containers stay stateless except PostgreSQL, so a move to ECS and RDS changes infrastructure
    code, not application code.
- **Accepted**:
  - One host is a single point of failure; the pilot is not highly available.
  - Releases interrupt service for seconds while containers restart.
  - Up to 24 hours of data can be lost if the data volume is lost (more if a failed nightly backup
    is not fixed before the next).
  - The developer patches the host (unattended security upgrades, rebooting at 04:30 UTC when an
    update requires it).
- **Revisit when**:
  - the pilot needs availability beyond one host, or releases must not interrupt service: move the
    containers to ECS on Fargate behind a load balancer;
  - losing up to a day of data, or a recovery of up to 2 hours, becomes unacceptable: move the
    database to RDS with point-in-time recovery;
  - the API and the worker need to scale separately; or
  - the load test shows a `t4g.small` is enough, or a `t4g.medium` is not.

Details: `specs/004-pilot-deployment/research.md` R1 to R3, R12, and R19.
