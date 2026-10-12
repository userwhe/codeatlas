# Operations Contract: Pilot Deployment

The interfaces the developer operates: environments, services, variables, commands, the release
flow, the external monitor, logs, and metrics. Decisions and rationale are in
[research.md](../research.md); step-by-step procedures go in `docs/operations.md`.

## Environments

One Railway project, `codeatlas`, in the US East region.

| Environment | Made by | Lifetime | Mode | Monitors and alerts |
| --- | --- | --- | --- | --- |
| `production` (the pilot) | `railway config apply` from `.railway/railway.ts` | Always on | `CODEATLAS_ENV=production` | Yes |
| `drill` | `railway environment new drill --duplicate production` | A recovery exercise | Production, with its own domain and secrets set again; no worker deployment | No |
| `loadtest` | `railway environment new loadtest --duplicate production` | A load test | Development with fake externals; no backup schedule | No |

A duplicated environment copies the services, configuration, and non-secret variables, gets empty
volumes and a new generated domain, and does not receive sealed variables. Delete it with
`railway environment delete <name>` after use.

## Services

| Service | Source and root directory | Start command | Pre-deploy | Healthcheck | Restart policy | Public domain | Size limit |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `web` | `main`, `frontend/`, `Dockerfile` (stage `production`) | `node server.js` | `node scripts/wait-for-api.mjs` | `/` | On failure, 10 retries | Yes | 0.5 vCPU, 0.5 GB |
| `api` | `main`, `backend/`, `Dockerfile` | `uvicorn codeatlas.api.app:app --host 0.0.0.0 --port 8000` | `python -m codeatlas.ops migrate` | `/readyz` | On failure, 10 retries | No | 1 vCPU, 1 GB |
| `worker` | `main`, `backend/`, `Dockerfile` | `python -m codeatlas.jobs.worker` | `python -m codeatlas.ops wait-for-api` | none | On failure, 10 retries | No | 1 vCPU, 2 GB |
| `backup` | `main`, `backend/`, `Dockerfile` | `python -m codeatlas.ops backup`, cron `30 3 * * *` (UTC) | `python -m codeatlas.ops wait-for-api` | none | Never | No | 0.5 vCPU, 0.5 GB |
| `db` | Image `pgvector/pgvector:pg17`; volume at `/var/lib/postgresql/data` | `docker-entrypoint.sh postgres -c shared_buffers=256MB -c max_connections=50` (a start command replaces the image's entrypoint) | none | none | On failure, 10 retries | No, and no TCP proxy | 1 vCPU, 1 GB |
| `backups` | Railway bucket in the separate project `codeatlas-backups`, created once by the developer | n/a | n/a | n/a | n/a | No | n/a |

`web`, `api`, `worker`, and `backup` deploy from `main` with "Wait for CI" on from the first
deployment. Pull request environments are off. The pre-deploy commands of `web`, `worker`, and
`backup` are the release gate: each waits, for up to 20 minutes, until `api` reports the
deployment's own commit at `/version` (research R7); their pre-deploy timeout is 1,500 seconds.

## Variables

Set in `.railway/railway.ts`; secrets are declared there with `preserve()`, set with the CLI
(`railway variable set NAME --stdin --skip-deploys`), and sealed in the dashboard, never in the
repository. "Ref" marks a reference variable. Sealed values are not copied into duplicated
environments.

| Variable | Services | Value |
| --- | --- | --- |
| `CODEATLAS_ENV` | api, worker | `production` (`development` in `loadtest`) |
| `CODEATLAS_FAKE_EXTERNALS` | api, worker | `0` (`1` in `loadtest`) |
| `APP_ORIGIN` | api, worker | Ref: `https://${{web.RAILWAY_PUBLIC_DOMAIN}}` |
| `DATABASE_URL` | api, worker, backup | Ref: `postgresql+psycopg://codeatlas:${{db.POSTGRES_PASSWORD}}@${{db.RAILWAY_PRIVATE_DOMAIN}}:5432/codeatlas` |
| `API_ORIGIN` | web (build argument and run time), worker, backup | Ref: `http://${{api.RAILWAY_PRIVATE_DOMAIN}}:8000` |
| `PORT` | web, api | `3000`, `8000` |
| `CLIENT_ADDRESS_HEADER` | api | `X-Real-IP` |
| `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_CLIENT_ID` | api, worker | The pilot App's values, set on Railway (not sealed) after the App is registered; the file declares them with `preserve()` |
| `GITHUB_APP_CLIENT_SECRET`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET` | api, worker | Sealed |
| `TOKEN_ENCRYPTION_KEY`, `GEMINI_API_KEY`, `VOYAGE_API_KEY` | api, worker | Sealed |
| `POSTGRES_USER`, `POSTGRES_DB` | db | `codeatlas` |
| `POSTGRES_PASSWORD` | db | Sealed; generated once |
| `PGDATA` | db | `/var/lib/postgresql/data/pgdata` |
| `BACKUP_ENDPOINT_URL`, `BACKUP_REGION`, `BACKUP_BUCKET`, `BACKUP_ACCESS_KEY_ID`, `BACKUP_SECRET_ACCESS_KEY` | backup | Sealed: copied from the credentials of the bucket `backups` in `codeatlas-backups` |
| `BACKUP_RETENTION_DAYS` | backup | `7` |
| `RAILWAY_DEPLOYMENT_OVERLAP_SECONDS` | web, api | `10` |
| `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` | web, api; worker | `10`; `30` |
| Limits and thresholds: `DAILY_QUESTION_LIMIT`, `DAILY_REVIEW_LIMIT`, `PILOT_DAILY_QUESTION_LIMIT`, `PILOT_DAILY_REVIEW_LIMIT`, `RATE_LIMIT_PER_MINUTE`, `WORKER_SILENT_SECONDS`, `JOBS_WAITING_ALERT_SECONDS`, `JOBS_FAILING_ALERT_COUNT`, `BACKUP_MISSING_HOURS`, `INDEXING_DEADLINE` | api, worker | Unset (the defaults in the data model), except for temporary changes and the load test |

Railway provides `RAILWAY_GIT_COMMIT_SHA` to every build and deployment from GitHub; the API reads
it as the release, and `web`'s Dockerfile declares it as a build argument for the footer.

## Settings outside the infrastructure file

Recorded in `docs/operations.md` with their values, because `.railway/railway.ts` cannot hold
them:

| Setting | Where | Value |
| --- | --- | --- |
| The web app's generated domain | `web`, Settings, Networking (or `railway domain`) | `codeatlas.up.railway.app`, or the nearest free name |
| Replica size limits, if `railway config plan` does not show them | Each service, Settings, Deploy | The sizes in "Services" |
| Cron schedule, if `railway config plan` does not show it | `backup`, Settings, Deploy | `30 3 * * *` |
| "Wait for CI", if `railway config plan` does not show it | `web`, `api`, `worker`, `backup`, Settings, Source | On, before the first deployment |
| Pre-deploy timeout, if `railway config plan` does not show it | `web`, `worker`, `backup`, Settings, Deploy | 1,500 seconds |
| Notification rules | Account, Notifications, with a project override | Email for build failed, deployment failed, and deployment crashed |
| Usage limits | Workspace, Usage | Custom email alert $20; hard limit $50 |
| SSH key | Account, SSH keys | The developer's public key |

## Operator commands

Run in a service's container through the platform's SSH:
`railway ssh -s <service> -- python -m codeatlas.ops <command>`. The service is `api` unless
noted.

| Command | Effect | Exit status |
| --- | --- | --- |
| `pilot-users add <login> [--note TEXT]` | Resolves the login to a GitHub user ID and adds it; audit `pilot_user_add` | 0; 1 if the login does not exist, is already listed, or the list is full |
| `pilot-users remove <login>` | Deletes the row and revokes the user's sessions; audit `pilot_user_remove` | 0; 1 if not listed |
| `pilot-users delete-data <login>` | For a user not on the list: disconnects all their repositories, deletes their stored GitHub tokens, revokes sessions; audit `pilot_user_delete_data` | 0; 1 if the user is still listed or unknown |
| `pilot-users list` | Prints login, user ID, note, and date added | 0 |
| `wait-for-api` | The release gate of `worker` and `backup`: polls `API_ORIGIN/version` every 5 seconds until its `commit` equals `RAILWAY_GIT_COMMIT_SHA`; passes at once when that variable is unset | 0 when it matches or no commit is set; 1 after 20 minutes |
| `migrate` | `api`'s pre-deploy command. Under an advisory lock: `alembic upgrade head` when the database's revision is known to this release or absent; otherwise logs `migrations_skipped` with both revisions | 0 migrated or skipped; 1 if a migration fails |
| `backup` | The `backup` service's cron command: first pruning of the backups dated before the last `BACKUP_RETENTION_DAYS` UTC days (objects, then rows), whatever happens next; then dump and counts in one snapshot, `pg_restore --list` check, manifest, upload of the dump and then the manifest under `backups/<UTC date>/<UTC time>/` (stopping if those keys exist), and a `backups` row | 0; 1 on any failure after pruning, with no row written |
| `restore <backup> [--yes]` | `<backup>` is a UTC date (the newest backup of that day with a manifest) or `<UTC date>/<UTC time>`. Downloads the dump and manifest, checks the SHA-256, asks for confirmation unless `--yes`, ends every other database session, drops and recreates the `public` schema, runs `pg_restore --no-owner --exit-on-error`, runs the `verify-restore` comparison, and then migrates the schema as `migrate` does. Needs the `BACKUP_*` variables exported in the SSH session; the guide stops `web` and `worker` first | 0 restored, verified, and migrated; 1 on a missing setting, a missing backup, a checksum mismatch, a restore error, a verification difference, or a failed migration |
| `verify-restore MANIFEST` | Compares the database's Alembic revision and row counts with a manifest file; prints differences | 0 when equal; 1 otherwise |

`backup-manifest` is removed: `backup` builds the manifest itself.

## Release flow

| Aspect | Contract |
| --- | --- |
| Trigger | A push to `main` (a merged pull request) whose GitHub Actions check suites all pass; Railway waits up to 2 hours, and skips the deployment if a suite fails |
| Order per service | Build from the commit; the pre-deploy command in its own container (`migrate` for `api`, the release gate for the others); start; for `web` and `api`, traffic moves only after the healthcheck returns 2xx within 300 seconds |
| Failure | A failed build, pre-deploy command, or healthcheck marks the deployment failed; the previous deployment keeps serving; the developer is emailed. When `api` fails, the other services' gates time out, so the whole release stops |
| Old deployment | `web` and `api` overlap for 10 seconds, then get `SIGTERM` and 10 seconds before `SIGKILL`; `worker` gets `SIGTERM` and 30 seconds |
| Schema report | CI's `schema` job on each pull request lists every added, modified, deleted, renamed, or copied file under `backend/alembic/versions/`, with its status, in its summary and as a warning annotation; it never fails |
| Rollback | A service's Deployments list, "Rollback" on the previous deployment: same image and variables, no rebuild, serving within 5 minutes. A release is rolled back as a whole, `api` first. Within 72 hours of that deployment's replacement on Hobby; later, redeploy the earlier commit, serving within 15 minutes |
| Credentials | None in the repository or CI; the platform's GitHub integration pulls the repository |

## External monitor (Better Stack, free plan)

Checks every 3 minutes from outside the platform; email on failure and on recovery.

| Monitor | URL | Alerts when |
| --- | --- | --- |
| Web app | `https://<domain>/` | Not 2xx for 5 minutes (confirmation period 5 minutes) |
| API and database | `https://<domain>/readyz` | Not 2xx for 5 minutes (confirmation period 5 minutes) |
| Worker | `https://<domain>/status/worker` | 503 or no answer |
| Waiting jobs | `https://<domain>/status/jobs-waiting` | 503 or no answer |
| Failing jobs | `https://<domain>/status/jobs-failing` | 503 or no answer |
| Backups | `https://<domain>/status/backup` | 503 or no answer; created after the first backup completes |

## Logs

Railway's Log Explorer for the environment, 7 days on Hobby. Filters: `@service:<name>`,
`@level:error`, `@request_id:<id>`, `@job_id:<id>`, `@run_id:<id>`, `@snapshot_id:<id>`; HTTP logs
by `@httpStatus` and `@totalDuration`.

Structured lines: `request` (`method`, `route`, `status`, `duration_ms`); `job_finished` (`kind`,
`outcome`, `attempt`, `duration_ms`); provider warnings (`provider`); `worker_status` every 60
seconds (`queued_jobs`, `oldest_runnable_job_age_seconds`); `migrations_skipped`
(`database_revision`, `release_revision`); the release gate's `release_gate_waiting` (`commit`,
`reported_commit`); and the backup command's `backup_completed` (`object_key`, `bytes`) and
`backup_pruned` (`pruned`).

## Metrics

Railway's per-service CPU, memory, disk, and network (up to 30 days), and `railway metrics --http`
for request counts, status codes, and latency percentiles at the edge. Queue and job numbers come
from the log lines above.

## Load test workflow (`.github/workflows/load-test.yml`)

`workflow_dispatch` inputs: `base_url`, `levels` (default `10,20,40,80`), `duration` (seconds per
level, default 120). It measures the network baseline against `/version`, runs `perf_check --json`
per level, stops at the first failing level, and uploads the JSON results and the rendered
Markdown table as an artifact.
