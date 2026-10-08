# Operations Contract: Pilot Deployment

The interfaces the developer operates: environments, configuration, commands, scripts, the
release workflow, logs, metrics, and alarms. Decisions and rationale are in
[research.md](../research.md); step-by-step procedures go in `docs/operations.md`.

## Environments

| Environment | Built from | Lifetime | Mode | Alarms and dashboard |
| --- | --- | --- | --- | --- |
| `pilot` | `infra/stack/` with `pilot.tfvars` | Always on | `CODEATLAS_ENV=production` | Yes |
| `drill` | `infra/stack/` with `drill.tfvars` | A recovery exercise | Production, reading the pilot's parameters, with its own `APP_ORIGIN`; no worker | No |
| `loadtest` | `infra/stack/` with `loadtest.tfvars` | A load test | Development with fake externals | No |

Each has its own domain name, Terraform state key, log groups, and data volume. Only the pilot's
data volume is protected by `prevent_destroy`. Shared resources come from `infra/shared/`.

## Terraform

`infra/shared/` variables: `region` (default `us-east-1`), `domain` (the hosted zone),
`github_oidc_subject_prefix` (the repository's OIDC subject prefix, read with
`gh api repos/<owner>/<repo>/actions/oidc/customization/sub`, for example
`repo:<owner>@<owner ID>/<repo>@<repo ID>`), `alert_email`, `monthly_budget_usd` (default 40).
Outputs: `ecr_repositories`, `bucket`, `alert_topic_arn`, `release_role_arn`, `zone_id`,
`name_servers`.

`infra/stack/` variables: `environment` (`pilot`, `drill`, or `loadtest`), `hostname`, `domain`,
`availability_zone` (default `us-east-1a`), `instance_type` (default `t4g.medium`),
`data_volume_gib` (default 20), `root_volume_gib` (default 20), `parameters_path` (default
`/codeatlas/<environment>`).
Outputs: `instance_id`, `public_ip`, `url`.

Both modules require Terraform 1.11 or later and read their state location from
`-backend-config=backend.hcl`. Workflow: `terraform plan -var-file=<env>.tfvars -out=plan.out`,
review it, then `terraform apply plan.out`. Real `*.tfvars` and `backend.hcl` files are not
committed; the `.example` files are.

## SSM parameters (`/codeatlas/<environment>/`)

Written by `deploy/put-secrets.sh <environment>` (`--set NAME=VALUE` and `--unset NAME` change one
optional setting; `--overwrite NAME` replaces a secret, except `POSTGRES_PASSWORD`, whose
replacement needs `ALTER ROLE` first, as the operations guide describes); read on the host by
`deploy/render-config.sh`.

| Name | Type | Required when | Used by |
| --- | --- | --- | --- |
| `CODEATLAS_ENV`, `CODEATLAS_FAKE_EXTERNALS` | String | Always | api, worker, migrate |
| `TOKEN_ENCRYPTION_KEY` | SecureString | Always | api, worker, migrate |
| `POSTGRES_PASSWORD` | SecureString | Always | db; api, worker, and migrate through `DATABASE_URL` |
| `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_CLIENT_ID` | String | Production | api, worker, migrate |
| `GITHUB_APP_CLIENT_SECRET`, `GITHUB_WEBHOOK_SECRET` | SecureString | Production | api, worker, migrate |
| `GITHUB_APP_PRIVATE_KEY` | SecureString | Production | Written to a file; `GITHUB_APP_PRIVATE_KEY_PATH` points to it |
| `GEMINI_API_KEY`, `VOYAGE_API_KEY` | SecureString | Production | api, worker, migrate |
| Limits and switches: `DAILY_QUESTION_LIMIT`, `DAILY_REVIEW_LIMIT`, `PILOT_DAILY_QUESTION_LIMIT`, `PILOT_DAILY_REVIEW_LIMIT`, `RATE_LIMIT_PER_MINUTE`, `EMIT_METRICS` | String | Optional | api, worker |

"Production" means `CODEATLAS_ENV=production`. Derived by render-config, never stored in SSM:
`APP_ORIGIN=https://<hostname>`, `DATABASE_URL`, `GITHUB_APP_PRIVATE_KEY_PATH`, and
`METRICS_ENVIRONMENT=<environment>`. Outside the pilot it forces `EMIT_METRICS=0`. Every value is
written double-quoted, with `\`, `"`, and `$` escaped (`$` as `$$`), so Compose reads it back
exactly; Compose's env-file parser does not accept the shell's `'\''` form inside single quotes.

## Host layout

| Path | Contents |
| --- | --- |
| `/opt/codeatlas/environment` | Written by cloud-init: `ENVIRONMENT`, `AWS_REGION`, `REGISTRY`, `BUCKET`, `CODEATLAS_HOSTNAME`, `PARAMETERS_PATH` |
| `/opt/codeatlas/releases/<sha>/` | One extracted release bundle (the `deploy/` directory) per release |
| `/opt/codeatlas/current` | Symbolic link to the running release's bundle |
| `/opt/codeatlas/.env` | Compose interpolation variables, written by `release.sh` only after a successful migration |
| `/opt/codeatlas/runtime/` | `app.env` (0600), `db.env` (0600), `github-app.pem` (0400, UID 1000) |
| `/usr/local/bin/codeatlas-compose` | `docker compose --project-directory /opt/codeatlas -f /opt/codeatlas/current/compose.yml --env-file /opt/codeatlas/.env "$@"` |
| `/var/lib/codeatlas/` (data volume) | `postgres/`, `caddy/` (certificates), `backup/` (staging), `state/running` and `state/previous` (commits) |
| `/var/log/codeatlas/` | Host script logs, shipped to `/codeatlas/<environment>/host` |

## Operator commands

Run on the host through Session Manager:
`codeatlas-compose run --rm api python -m codeatlas.ops <command>`.

| Command | Effect | Exit status |
| --- | --- | --- |
| `pilot-users add <login> [--note TEXT]` | Resolves the login to a GitHub user ID and adds it; audit `pilot_user_add` | 0; 1 if the login does not exist, is already listed, or the list is full |
| `pilot-users remove <login>` | Deletes the row and revokes the user's sessions; audit `pilot_user_remove` | 0; 1 if not listed |
| `pilot-users delete-data <login>` | For a user not on the list: disconnects all their repositories, deletes their stored GitHub tokens, revokes sessions; audit `pilot_user_delete_data` | 0; 1 if the user is still listed or unknown |
| `pilot-users list` | Prints login, user ID, note, and date added | 0 |
| `backup-manifest --counts FILE --dump-key KEY --bytes N --sha256 HEX` | Prints the manifest JSON (data model) built from the counts file | 0; 1 if the counts file is invalid |
| `verify-restore MANIFEST` | Compares the restored database's Alembic revision and row counts with the manifest; prints differences | 0 when equal; 1 otherwise |

The two backup commands read files under `/var/lib/codeatlas/backup`, so the scripts run them with
`codeatlas-compose run --rm -v /var/lib/codeatlas/backup:/backup:ro api ...` and pass `/backup/...`
paths.

## Host scripts (from the release bundle, run as `/opt/codeatlas/current/<script>`)

| Script | Arguments | Behavior | Exit status |
| --- | --- | --- | --- |
| `release.sh` | `<sha>` `[--expect-version V]` `[--refresh-config]` | Run from `/opt/codeatlas/releases/<sha>/`. Renders configuration, pulls the `api` and `web` images, migrates, then writes `.env`, switches `current`, starts the new tag (recreating `caddy` when the Caddyfile changed), checks `/readyz`, `/`, and `/version`, and rolls back on failure. `--refresh-config` re-renders configuration and recreates `api` and `worker` on the running tag | 0 released; 2 any failure before the switch (rendering, login, pull, migration), previous version serving; 3 check failed, rolled back; 4 rollback failed, or check failed with no previous release |
| `backup.sh` | none (systemd timer, 03:30 UTC) | Dump and counts in one snapshot (stopping if `pg_dump` fails), `pg_restore --list` check, manifest, upload with `If-None-Match`, `BackupCompleted` metric | 0; non-zero on any failure, with no metric |
| `restore.sh` | `<UTC date>` | Download, checksum, stop `api` and `worker`, `pg_restore`, `verify-restore` | 0; non-zero on checksum, restore, or verification failure |
| `check-certificate.sh` | none (systemd timer, daily 06:00 UTC) | Publishes `CertificateDaysLeft` for the environment's domain | 0; non-zero if the certificate cannot be read |
| `render-config.sh` | `<environment>` | Writes `runtime/` from SSM and derived settings, escaping `$` as `$$` | 0; non-zero naming each missing required parameter |

`deploy/put-secrets.sh <environment>` runs on the developer's machine, not the host.

## Release workflow (`.github/workflows/release.yml`)

| Aspect | Contract |
| --- | --- |
| Triggers | `workflow_run` after CI completes; `workflow_dispatch` with inputs `commit` (a `main` commit; default the head) and `expect_version` (default empty) |
| Gate | The `workflow_run` trigger is filtered to `branches: [main]`. Every job requires a dispatch, or a CI run that concluded `success` for a `push` to this repository's `main` (`event`, `head_repository`, and `head_branch` checked); jobs check out `head_sha`. A dispatched commit must be an ancestor of `origin/main` with a successful CI run |
| Jobs | `build` (no cloud credentials: image tarballs as artifacts kept 3 days), `summary` (release summary in the run; `actions: read`), `release` (on `ubuntu-24.04`, environment `pilot`; after approval: push images, upload bundle, Run Command) |
| Approval | The `pilot` environment requires the developer's review and allows only `main` |
| Credentials | One OIDC role, `codeatlas-release`, trusted for `<github_oidc_subject_prefix>:environment:pilot`: ECR push to the two repositories, S3 `releases/*` upload, Run Command on the pilot instance, command results, SNS publish; no stored AWS keys |
| Summary | Commit, commits since the running version, new files under `backend/alembic/versions/` |
| Outputs | Deployment status in GitHub; longest outage of `/readyz` or `/` during the release; alert on failure |
| Concurrency | `release-pilot` on the `release` job only, so runs whose jobs are skipped never join it; a job waiting for approval blocks newer ones until it is approved or rejected |

## Load test workflow (`.github/workflows/load-test.yml`)

`workflow_dispatch` inputs: `base_url`, `levels` (default `10,20,40,80`), `duration` (seconds per
level, default 120). It runs `perf_check --json` per level, stops at the first failing level, and
uploads the JSON results and the rendered Markdown table as an artifact.

## Log groups (7-day retention)

`/codeatlas/<environment>/{caddy,web,api,worker,db,releases,host}`.

Structured fields used by queries: `request_id`, `job_id`, `run_id`, `snapshot_id`; on `request`
lines `method`, `route`, `status`, `duration_ms`; on `job_finished` lines `kind`, `outcome`,
`attempt`, `duration_ms`; on provider errors `provider`.

## Metrics (namespace `CodeAtlas`, dimension `Environment`)

| Metric | Source | Unit |
| --- | --- | --- |
| `WorkerHeartbeat` | Worker's reporter thread, every 60 s | Count (1) |
| `QueuedJobs` | Worker's reporter thread, every 60 s: jobs `queued` or `retry_wait` | Count |
| `OldestRunnableJobAgeSeconds` | Worker's reporter thread, every 60 s: now minus `run_after` of the oldest job `claim_next` could claim now (waiting, `run_after` passed, no running job of the same workspace and kind) | Seconds |
| `JobsFailed` | Where a job is marked failed, when retries were used up, it timed out, or the code is `internal_error` | Count |
| `BackupCompleted` | `backup.sh` | Count (1) |
| `CertificateDaysLeft` | `check-certificate.sh` | Count (days) |
| `mem_used_percent`, `disk_used_percent` (root, data; dimensions `InstanceId`, `path`, `fstype`) | CloudWatch agent (namespace `CWAgent`) | Percent |

## Health checks and alarms (pilot)

Route 53 HTTPS health checks of `/readyz` and `/`, every 30 seconds.

Alarms: `site-down`, `web-down`, `worker-down`, `jobs-waiting`, `jobs-failing`, `root-disk-full`,
`data-disk-full`, `backup-missing`, `certificate-expiring`, and `host-status-check`, with
conditions in research R10. Each notifies the SNS topic `codeatlas-alerts` when it fires and when
it recovers. The budget `codeatlas-monthly` emails once at 80% and 100% of actual spend and at 80%
of forecast spend (forecasts start after about five weeks of data).
