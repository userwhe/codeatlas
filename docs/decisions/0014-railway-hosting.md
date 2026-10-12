# ADR 0014: The pilot runs on Railway

- **Status**: Accepted
- **Date**: 2026-10-12
- **Scope**: `specs/004-pilot-deployment`; supersedes ADR 0010

## Context

The pilot serves at most 10 invited users. Its processes are the web app, the API, the worker, and
PostgreSQL with pgvector (ADR 0001, ADR 0002), already built and tested together in Docker
Compose. ADR 0010 put them on one EC2 host defined with Terraform. Before anything was deployed,
the developer gave that up: the host needed patching, a domain had to be bought, and the AWS
account's organization policies blocked both the release pipeline and the external checks. The
revised spec asks for the platform's own HTTPS address with no domain, a recorded and previewed
configuration, daily backups to storage separate from the database, and hosting costs of at most
$25 a month.

## Options considered

1. Railway: services built from the repository's Dockerfiles, a private network, a volume, a
   bucket, cron, logs, metrics, and infrastructure as code.
2. One EC2 host with Docker Compose (ADR 0010).
3. Render: fixed-price services and managed PostgreSQL with pgvector.
4. Fly.io: VMs, with PostgreSQL self-run or on a managed plan.
5. Vercel for the web app and Railway for the rest.

## Decision

Option 1:

- One project on the Hobby plan, in US East. The environment `production` is the pilot; `drill`
  and `loadtest` are temporary copies.
- Services, one replica each: `web` (the only public service, at its `*.up.railway.app` domain),
  `api`, `worker`, `backup` (a nightly cron service), and `db` (`pgvector/pgvector:pg17` with a
  volume, no public domain or TCP proxy), plus a `backups` bucket. The backend's one image serves
  `api`, `worker`, and `backup`; it gains Debian's `postgresql-client-17`.
- The web app forwards the API's paths over the private network, so the browser, GitHub's
  webhooks, and the OAuth callback use one origin.
- Configuration lives in `.railway/railway.ts` (Railway's infrastructure as code). The developer
  previews it with `railway config plan` and applies it with `railway config apply`. Secrets are
  sealed variables declared with `preserve()`, so their values are never in the file. Reference
  variables derive each environment's origin, database URL, and bucket settings. Settings the file
  cannot hold (the generated domain, size limits, notification rules, usage limits) are recorded in
  the operations guide. Railway's older `railway.json` is deprecated and not used.
- Nightly backups are logical dumps with row counts, uploaded to the bucket by
  `python -m codeatlas.ops backup` through `boto3`, a new dependency for the bucket's S3-compatible
  API, and kept 7 days.

## Trade-off

- **Gained**:
  - No host to patch, no SSH, no certificates, no domain, and no Terraform; about $13 a month.
  - The deployed images are the ones CI builds, from the same Dockerfiles, with the same
    PostgreSQL image as development.
  - A copied environment is a working copy of the configuration, which the load test and the
    recovery exercise use.
- **Accepted**:
  - Each service is a single replica on one platform; a platform outage takes the pilot down.
  - The Hobby plan caps the database volume at 5 GB and keeps logs for 7 days.
  - The backups live in the same Railway account as the database, and the bucket's one key pair
    can delete them; losing the account loses both.
  - Part of the configuration stays outside the file, and the infrastructure as code tool is new.
  - The configuration is specific to Railway; moving elsewhere means rewriting it, though not the
    application.
- **Revisit when**:
  - the pilot needs more than one replica or region;
  - the database passes 3 GB, or 7 days of logs are not enough: consider the Pro plan;
  - losing the backups with the account becomes unacceptable: copy them to storage in another
    account; or
  - the monthly bill passes $25.

Details: `specs/004-pilot-deployment/research.md` R1 to R5, R8, R12, R19, and R20.
