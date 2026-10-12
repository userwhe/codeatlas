# Research: Pilot Deployment

Phase 0 output for [plan.md](plan.md). Each entry records the decision, the rationale, and the
alternatives considered. This feature builds on `specs/001-repository-qa`,
`specs/002-push-reindexing`, and `specs/003-pr-review`; references such as "001 R11" point to
their research notes.

On 2026-10-12 the developer replaced the single AWS host with Railway (spec Input). The earlier
design (one EC2 host, Terraform, approved releases, CloudWatch) is recorded in ADRs 0010 to 0012,
now superseded, and its code stays in the history at commit `c0b0e22`. R20 lists what it leaves
behind. Railway facts were checked against docs.railway.com, its CLI reference, and Railway staff
answers on station.railway.com on 2026-10-12; prices come from Railway's pricing page on the same
day. R19 adds them up.

## Unknowns resolved

| Unknown | Resolution |
| --- | --- |
| Where and on what the pilot runs | R1, ADR 0014 |
| How the services are built, started, and restarted | R2 |
| The public address, routing, and what the internet can reach | R3 |
| Rate limits, and which client address they count | R4 |
| Settings and credentials on the platform | R5 |
| How a release is labeled with its commit | R6 |
| How a release is started, checked, and rolled back | R7, ADR 0015 |
| How the configuration is recorded and changed | R8, ADR 0014 |
| Where logs go and how they are searched | R9 |
| Metrics and the alerts the platform cannot send | R10, ADR 0016 |
| Liveness, readiness, and the running version | R11 |
| Backups, restore, and recovery | R12 |
| The pilot access list | R13, ADR 0013 |
| Pilot-wide daily limits and the worst-case model spend | R14, ADR 0013 |
| How and where the load test runs | R15 |
| The benchmark evaluation and the project's own sets | R16 |
| README, diagram, recording, and operations guide | R17 |
| Tests and checks | R18 |
| Monthly cost | R19 |
| What happens to the AWS design | R20 |

New tools and services, each tied to a requirement: Railway (hosting, builds, logs, metrics,
volumes, buckets, cron, notifications, usage limits; R1 to R12), Railway's infrastructure as code
(FR-016, FR-025; R8), and Better Stack's free uptime monitoring (FR-014; R10). The backend gains one
Python dependency, `boto3`, for the S3-compatible backup bucket (R12), and its image gains the
PostgreSQL 17 client tools (`pg_dump`, `pg_restore`). The frontend gains no npm dependency.

## Deviations from `docs/design/codeatlas-v1.md`

| Design document | This increment | Revisit when |
| --- | --- | --- |
| ECS on Fargate behind an HTTPS load balancer | Railway services from the repository's Dockerfiles, one replica each, behind the platform's edge (R1, R2, ADR 0014) | Availability needs more than one replica, or the API and worker need to scale separately |
| A private RDS PostgreSQL instance | PostgreSQL 17 with pgvector as a Railway service with a volume, private network only, and nightly logical backups to a Railway bucket (R12) | Up to 24 hours of data loss or a 2-hour recovery becomes unacceptable, or the data outgrows a 5 GB volume |
| SQS, a transactional outbox, and S3 artifact storage | Unchanged from 001: the PostgreSQL job queue (ADR 0003) and PostgreSQL as the only data store (ADR 0002). The bucket holds only backups | ADR 0002 and ADR 0003 revisit conditions |
| A staging environment with a smoke test | No standing staging. CI runs the browser tests in fake mode; each service takes traffic only after its readiness check; copied environments serve the load test and the recovery exercise (R7, R15, R12) | Releases start to fail their readiness check, or a bad release reaches users |
| Traces | Logs carry request, job, run, and snapshot identifiers; no tracing (R9) | A request path spans more than the web app, the API, and the database |
| All listed metrics as metrics | The platform's resource and HTTP metrics, and structured log lines for queue and job numbers (R10, ADR 0016) | An alert is needed on a number that only the logs hold |
| An operations view for jobs | None; the status endpoints, the logs, and the `jobs` table | The developer needs it more than weekly |
| Distinct API and worker IAM roles | No service has cloud credentials; only the backup service holds the bucket's keys (R5, R12) | A service needs another external store |
| Edge rate limiting | An in-process limiter in the API, before any other processing (R4) | The API runs more than one replica |

## R1. Hosting: Railway (ADR 0014)

**Decision**:

- **Platform**: Railway, Hobby plan ($5 a month, including $5 of usage), one project `codeatlas`
  with the environment `production` (the pilot), in the US East region. Temporary environments,
  `drill` and `loadtest`, are copies of `production` (R12, R15).
- **Services** (one replica each):

  | Service | Source | Runs | Public | Size limit (vCPU, memory) |
  | --- | --- | --- | --- | --- |
  | `web` | `frontend/Dockerfile`, last stage `production` | Next.js standalone server | Yes, the platform's domain | 0.5, 0.5 GB |
  | `api` | `backend/Dockerfile` | uvicorn; pre-deploy `python -m codeatlas.ops migrate` | No | 1, 1 GB |
  | `worker` | `backend/Dockerfile` | `python -m codeatlas.jobs.worker` | No | 1, 2 GB |
  | `backup` | `backend/Dockerfile` | `python -m codeatlas.ops backup`, cron `30 3 * * *` (UTC) | No | 0.5, 0.5 GB |
  | `db` | image `pgvector/pgvector:pg17`, volume at `/var/lib/postgresql/data` | PostgreSQL 17 with pgvector | No | 1, 1 GB |
  | `backups` | Railway bucket | S3-compatible storage | No (credentials only) | n/a |

- **Limits**: Railway bills used CPU and memory, not limits. The limits above cap a runaway process
  and are the "service sizes" the load test reports (R15). The worker's 2 GB covers reading an
  archive of up to 100 MiB of eligible files (001 FR-009).
- **Database**: the same image as development and CI (`pgvector/pgvector:pg17`), because Railway's
  own PostgreSQL image has no pgvector, and its community pgvector template uses PostgreSQL 18.
  `PGDATA=/var/lib/postgresql/data/pgdata` (a volume's root holds `lost+found`, which `initdb`
  refuses); start command `postgres -c shared_buffers=256MB -c max_connections=50`. The service has
  no public domain and no TCP proxy, so only services in the same environment reach it, at
  `db.railway.internal:5432` (FR-007). Hobby volumes are capped at 5 GB.

**Rationale**:

- Railway runs the existing Dockerfiles with no host to administer: no OS patching, no SSH, no
  certificate handling, and no infrastructure to define beyond the services (spec Assumptions).
- It gives the web app a stable HTTPS address without a domain name (FR-001), private networking
  between services, a volume for PostgreSQL, a bucket for backups, cron for the nightly backup, a
  log explorer, metrics, deploy notifications, and spending limits, all in one place.
- The topology is the one tested in Compose and CI: the same images and the same PostgreSQL. The
  services stay stateless except `db`, so moving elsewhere later changes configuration, not the
  application.
- Usage pricing fits a pilot that is idle most of the day (R19).

**Alternatives considered**:

- **One EC2 host with Docker Compose** (ADRs 0010 to 0012, superseded): fixed cost about $34 a
  month, full control, but a host to patch, a domain to buy, Terraform to maintain, and an account
  whose organization policies blocked the release pipeline and the external checks.
- **Render**: fixed instance prices and managed PostgreSQL with pgvector, but about $27 a month for
  three services and the smallest database, and a free database that expires after 30 days.
- **Fly.io**: cheap compute, but PostgreSQL is either self-run on a VM or a managed plan near $40.
- **Vercel for the web app with Railway for the rest**: a free web tier and preview deployments,
  but two platforms, two sets of settings, a public API, and requests crossing providers. The
  worker cannot run on Vercel's functions.
- **Railway Pro** ($20 a month): platform volume backups (kept 6 days) and 30-day logs, but backups
  restore only into the same environment and are lost with the project. Revisit if the 5 GB volume
  cap or 7-day logs become limiting.

## R2. Services, builds, and restarts

**Decision**:

- **Builds**: Railway builds each service from its Dockerfile on every deployment, with the
  service's root directory as the build context (`backend/` or `frontend/`); no registry is
  involved. The backend's single stage serves `api`, `worker`, and `backup` with different start
  commands. The frontend's last stage, `production`, is the one Railway builds.
- **Backend image changes**: `postgresql-client-17` from Debian's own repository (the
  `python:3.13-slim` base is Debian 13, which ships PostgreSQL 17), for `ops backup` and
  `ops restore` (R12). The fixture repositories stay in the image for the load test's fake mode.
- **Listening**: environments created after 2025-10-16 have a dual-stack private network, so
  uvicorn keeps `--host 0.0.0.0` (on asyncio, `--host ::` would listen on IPv6 only). The web app
  keeps `HOSTNAME=0.0.0.0`. The API listens on port 8000, set as its `PORT` variable; the web app on
  3000.
- **Restart policy**: `ON_FAILURE` with up to 10 retries for `web`, `api`, and `worker`, so a crash
  restarts the service without manual steps (FR-017); after the retries, Railway emails a crash
  notice (R10). `backup` uses `NEVER`: a failed run waits for the next night, and its absence fires
  the backup alert after 26 hours.
- **Interrupted jobs**: as before, a job interrupted by a crash or a restart resumes through its
  lease: the lease lapses 60 seconds after the last heartbeat and another attempt claims it
  (001 FR-030).
- **No watch patterns**: every commit on `main` redeploys `web`, `api`, `worker`, and `backup`, so
  all services run the same commit and the reported version means one thing. Builds are free.

**Rationale**: One backend image for three services keeps one build to test in CI. Debian's
packaged client matches the server's major version, which `pg_dump` requires.

**Alternatives considered**:

- **A separate backup image** (for example `postgres:17` with a shell script): a second Dockerfile,
  and the manifest and verification logic, already in Python with unit tests, would be duplicated
  or rewritten.
- **Railway's builders (Railpack, Nixpacks)**: the Dockerfiles already exist and are tested in CI.
- **Watch patterns per service**: fewer redeploys, but services would then report different
  commits.

## R3. Edge: TLS, routing, and the public surface

**Decision**:

- **Address**: the web app's Railway domain, chosen once, for example `codeatlas.up.railway.app`
  (FR-001). Railway's edge terminates TLS with a Let's Encrypt certificate it renews itself, and
  answers plain HTTP with a 301 to HTTPS. The web app adds `Strict-Transport-Security:
  max-age=31536000` through `headers()` in `next.config.ts`, for every path.
- **Routing**: `next.config.ts`'s rewrites forward `/v1/*`, `/auth/*`, `/webhooks/*`, `/status/*`,
  `/readyz`, and `/version` to `API_ORIGIN`. Rewrites are fixed when Next.js builds, so `API_ORIGIN`
  becomes a build argument of the `production` stage, set from the reference variable
  `http://${{api.RAILWAY_PRIVATE_DOMAIN}}:8000` (R5). Development keeps `http://localhost:8000`.
- **Webhooks through the web app**: GitHub's deliveries reach the API through the same rewrite.
  The rewrite streams the body unchanged, so the HMAC signature still verifies. Next.js caps proxied
  bodies at 10 MB by default; push and pull request deliveries are far smaller, and a refused
  delivery is caught by the daily check (002).
- **Public surface** (FR-007, SC-005): only `web` has a public domain. `api`, `worker`, `backup`,
  and `db` have none, and `db` has no TCP proxy. Administration uses `railway ssh`, which
  authenticates with an SSH key registered on the developer's Railway account (R13).
- **Rebuilt environments**: a copied environment gets its own generated domain. The GitHub App's
  callback and webhook URLs point at the pilot's domain; the drill adds its own callback URL to
  the App for the exercise (R12). If the pilot's environment were lost, the developer renames the
  new environment's domain to the old name with `railway domain` when it is free, or updates the
  App's URLs (spec edge case).

**Rationale**: One public origin keeps the session cookie, the Origin check, and the OAuth
callback as they work in development, where Next.js already proxies these paths. No second
public service means no second address to protect.

**Alternatives considered**:

- **A public domain for the API as well**: simpler routing, but a second public surface, and the
  browser would talk to two origins.
- **Caddy in front of both** (the earlier design): an extra service to run for routing that
  Next.js already does.

## R4. Rate limits and the client address (FR-006)

**Decision**:

- The in-process limiter of the earlier design stays: an HTTP middleware in the API, before the
  Origin check and every route, allows 60 requests per minute per client address to `/auth/*`,
  `/webhooks/*`, and now `/status/*` (R10), and answers `429 rate_limited` with `Retry-After`
  before any other processing.
- **Client address**: requests reach the API from the web app over the private network, so the
  connection's address is the web app's. Railway's edge sets `X-Real-IP` on every request and
  overwrites any value the client sent (Railway staff, 2026-05-06); `X-Forwarded-For` is not
  documented and staff answers disagree on whether it is replaced or appended. The Next.js rewrite
  passes `X-Real-IP` through. A new setting, `CLIENT_ADDRESS_HEADER` (default empty), names the
  header to trust; the platform's variables set it to `X-Real-IP` on `api`. Without it, the API uses
  the connection's address, as in development and tests. The API has no public address, so the
  header can only come through the web app.
- A quickstart scenario checks the behavior on the platform: a request with a forged `X-Real-IP`
  is counted under the real address.

**Rationale**: The limiter needs one trustworthy value. The platform documents `X-Real-IP` as its
own and overwrites it, so it is the narrowest thing to trust, and the setting keeps development
and tests unchanged.

**Alternatives considered**:

- **uvicorn's proxy headers with `--forwarded-allow-ips='*'`**: it would read `X-Forwarded-For`,
  whose handling at the edge is undocumented, so a client might choose its own address.
- **A shared counter in PostgreSQL**: needed only when the API runs several replicas; the
  limiter's interface would stay the same.

## R5. Settings and credentials (FR-008 to FR-010)

**Decision**:

- **Store**: Railway variables, per service and per environment. Secrets are sealed: the platform
  gives them to the service's builds and deployments but never shows them again in the dashboard
  or API, and does not copy them into duplicated environments. Each service gets only the
  variables it uses:

  | Service | Variables |
  | --- | --- |
  | `api` | `CODEATLAS_ENV`, `CODEATLAS_FAKE_EXTERNALS`, `APP_ORIGIN`, `DATABASE_URL`, `PORT`, `CLIENT_ADDRESS_HEADER`, the GitHub App settings including `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET`, `TOKEN_ENCRYPTION_KEY`, `GEMINI_API_KEY`, `VOYAGE_API_KEY`, the limits and thresholds, and the deployment timings (R7) |
  | `worker` | The same as `api` except `PORT` and `CLIENT_ADDRESS_HEADER` |
  | `backup` | `DATABASE_URL`, the `BACKUP_*` settings from the bucket, and `BACKUP_RETENTION_DAYS`. No `CODEATLAS_ENV`: `ops backup` uses only the database, the release, and the bucket, so the production check, which requires the App and provider secrets, does not apply, and the service receives none of them |
  | `web` | `API_ORIGIN` (build argument), `PORT` |
  | `db` | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `PGDATA` |

- **Reference variables** keep values consistent and make a copied environment correct by
  itself: `APP_ORIGIN=https://${{web.RAILWAY_PUBLIC_DOMAIN}}`,
  `DATABASE_URL=postgresql+psycopg://codeatlas:${{db.POSTGRES_PASSWORD}}@${{db.RAILWAY_PRIVATE_DOMAIN}}:5432/codeatlas`,
  `API_ORIGIN=http://${{api.RAILWAY_PRIVATE_DOMAIN}}:8000`, and
  `BACKUP_ENDPOINT_URL=${{backups.ENDPOINT}}` with the bucket's `BUCKET`, `REGION`,
  `ACCESS_KEY_ID`, and `SECRET_ACCESS_KEY`.
- **Private key**: Railway variables are strings, and the key was read from a file. A new setting,
  `GITHUB_APP_PRIVATE_KEY`, holds the PEM itself (a multi-line variable). Production needs it or a
  readable `GITHUB_APP_PRIVATE_KEY_PATH`; the content wins when both are set. The GitHub client
  signs its JWT with whichever is configured.
- **Startup validation** (FR-009): unchanged from the earlier design and already implemented:
  every missing required setting is named in one error, with `hide_input_in_errors=True` so that
  no value is printed. The private key rule above replaces the readable-file rule.
- **Pilot GitHub App** (FR-010): a second App registration, `CodeAtlas`, with the same read-only
  permissions and events as the development App, the callback URL
  `https://<domain>/auth/github/callback`, the webhook URL `https://<domain>/webhooks/github`, and
  its own webhook secret. It is public, so pilot users can install it.
- **Replacing a credential**: set the new value on the services that use it (sealed again), which
  stages a change; deploying it restarts those services. Replacing `TOKEN_ENCRYPTION_KEY` makes
  stored GitHub tokens unreadable, and users sign in again, as 001 handles. `POSTGRES_PASSWORD`
  applies to the database only when its data directory is created, so the guide changes it with
  `ALTER ROLE` through `railway ssh -s db` first, then the variable; the reference variables carry
  it to `DATABASE_URL`.

**Rationale**: Sealed variables are the platform's own secret store, readable only by the services
that receive them, and never written to the repository, the images, or CI (FR-008). Reference
variables remove hand-copied values, which was the earlier design's render step.

**Alternatives considered**:

- **Shared variables for every secret**: fewer places to set them, but every service would get
  every secret.
- **An external secret manager**: another account and another credential to bootstrap.
- **Writing the key to a file at startup from a variable**: a start script for each service, where
  one setting does the same.

## R6. Release labeling

**Decision**:

- Railway gives every GitHub-triggered build and deployment `RAILWAY_GIT_COMMIT_SHA`. The API's
  `CODEATLAS_RELEASE` setting also reads it (`AliasChoices`), so `/version` reports the commit
  (FR-019) with no build argument. `CODEATLAS_RELEASE` still wins when set, for local builds.
- The web app inlines its footer's commit at build time: the `production` stage declares
  `ARG RAILWAY_GIT_COMMIT_SHA` and passes it to `NEXT_PUBLIC_RELEASE`.
- Railway's deployment history labels each deployment with its commit, which is the release record
  (data model).

**Rationale**: The platform already knows the commit; reading it removes the earlier design's
image tags and build arguments.

**Alternatives considered**: a `CODEATLAS_RELEASE` variable set per release (a manual step that can
drift); reading the commit from the Git checkout (not present in the image).

## R7. Releases (FR-019 to FR-024, ADR 0015)

**Decision**:

- **Trigger** (FR-019, FR-020): each app service (`web`, `api`, `worker`, `backup`) deploys from
  this repository's `main` branch through Railway's GitHub integration, with "Wait for CI" on. A
  push to `main` puts the deployment in a waiting state until every GitHub Actions check suite on
  the commit finishes; if one fails, Railway skips the deployment, and after 2 hours it gives up.
  The CI workflow already runs on pushes to `main`, which "Wait for CI" requires. Only the
  connected branch deploys; pull request environments stay off, and Railway never deploys pull
  requests from people outside the project. No approval step exists; merging a reviewed pull
  request is the developer's confirmation (Complexity Tracking in the plan).
- **Schema report** (FR-020): a CI job, `schema`, runs on pull requests. It lists the files the
  pull request adds or changes under `backend/alembic/versions/`, writes them to the job summary
  with the expand-then-contract rule (add in one release, remove in a later one), and raises a
  warning annotation on the pull request. It never fails the build.
- **Migrations first** (FR-021): `api`'s pre-deploy command, `python -m codeatlas.ops migrate`,
  runs in its own container with the service's variables and private network after the build and
  before the new version starts. A non-zero exit fails the deployment, and the previous version
  keeps serving. The command:
  - takes a PostgreSQL advisory lock, so two deployments never migrate at once;
  - runs `alembic upgrade head` when the database's revision is one this release knows (or there is
    none); and
  - when the database is ahead of the release (a rollback after a migration), logs
    `migrations_skipped` with both revisions and exits 0, because the schema rule keeps older
    versions working with newer schemas. Without this, the earlier design found that a rollback
    fails on "Can't locate revision".
- **Worker and schema**: `api`, `worker`, and `web` deploy in parallel, so the new worker may start
  before `api`'s migration finishes. At startup, the worker waits while the database's revision is
  older than its own (checking every 5 seconds, for up to 10 minutes, then exiting so the restart
  policy tries again), and starts at once when the revision equals its own or is newer.
- **Readiness gate** (FR-022): `api`'s healthcheck path is `/readyz` and `web`'s is `/`, with
  Railway's default 300-second timeout. Railway moves traffic to a new deployment only after its
  check returns 2xx; otherwise it marks the deployment failed and the previous one keeps serving.
  The developer receives the failure by email (R10).
- **Overlap and draining** (FR-023): `RAILWAY_DEPLOYMENT_OVERLAP_SECONDS=10` and
  `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=10` on `web` and `api` keep the old version answering
  in-flight requests after the switch. The worker has no healthcheck, so its new deployment is
  active as soon as it starts and the old one is stopped at once;
  `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=30` gives the old worker 30 seconds between `SIGTERM` and
  `SIGKILL`. The worker already stops after its current job on `SIGTERM` (001); a job still running
  at `SIGKILL` loses its lease within 60 seconds, and the new worker claims it as a new attempt.
  Fencing tokens prevent a duplicate publication (001 R3), so no job is lost or duplicated.
- **Interruption** (FR-023, SC-009): the readiness gate makes the switch nearly seamless for `web`
  and `api`. During each measured release, a probe on the developer's machine requests `/readyz`
  and `/` once a second and reports the longest run of failures (quickstart). `db` is not
  redeployed by releases; changing its image or volume causes a brief outage, which the operations
  guide schedules.
- **Mixed versions**: for a minute or so during a release, the web app and the API may run
  different commits. Changes that need both together follow the same expand-then-contract rule as
  the schema.
- **Rollback** (FR-024, SC-010): in a service's Deployments list, "Rollback" on the previous
  deployment redeploys its image and variables without a rebuild, in one action per service. The
  CLI has no rollback command. Hobby keeps a removed deployment's image for 72 hours; after that a
  rollback becomes a redeploy of the earlier commit (`railway redeploy` from a checkout, or a revert
  on `main`). Because `api`'s pre-deploy command skips migrations when the database is ahead, a
  rolled-back API starts against the newer schema.
- **Rehearsal** (SC-010): changing `api`'s healthcheck path to one that returns 404 and redeploying
  makes a release fail its readiness check without changing code; restoring the path ends the
  rehearsal.

**Rationale**: The developer chose releases without approval (spec Input). Railway's GitHub
integration, "Wait for CI", the pre-deploy command, and the healthcheck together give the earlier
pipeline's guarantees (main only, checks first, migrations first, readiness before traffic,
previous version kept on failure) without a release workflow, images, credentials in CI, or host
scripts.

**Alternatives considered**:

- **Approval before each release** (the earlier design): a release summary and a protected GitHub
  environment, but it needs a release workflow holding platform credentials, which FR-025 now
  forbids, and the developer chose not to approve each release.
- **Releasing from GitHub Actions with the Railway CLI**: explicit ordering of migrations and
  services, but a Railway token stored in CI.
- **A release only when a tag is pushed**: an extra manual step that "merge to `main`" already
  covers.
- **Migrating in the worker's pre-deploy command too**: two migrators racing; the advisory lock
  would make it safe but the worker's wait is simpler.

## R8. Recorded configuration (FR-016, FR-025)

**Decision**:

- **Infrastructure as code**: Railway's TypeScript infrastructure as code, generally available,
  in `.railway/railway.ts`. It declares the services (source, branch, root directory, Dockerfile,
  start and pre-deploy commands, healthcheck, restart policy, region and replicas, cron schedule),
  the `db` image and its volume, the `backups` bucket, and every variable: non-secret values and
  reference variables in the file, secrets declared with `preserve()` so that their values stay in
  the platform and never in the repository.
- **Preview and apply** (FR-025): the developer runs `railway config plan`, which shows every
  change as a diff with secret values redacted and destructive changes marked, and then
  `railway config apply`, which refuses a stale plan. The developer runs both from their machine,
  signed in with the Railway CLI; CI holds no Railway token, so the `railwayapp/config` GitHub
  Action is not used.
- **What stays outside the file**, recorded in the operations guide with its values: the generated
  domain (infrastructure as code manages only custom domains), replica size limits (documented
  only in the dashboard), notification rules and usage limits (account settings), SSH keys, the
  external monitor (R10), and the GitHub App. Settings changed in the dashboard appear as staged
  changes with an old-to-new diff before "Deploy" applies them; networking changes apply at once.
  The guide says to change settings in the file when the file can hold them.
- **Configuration as code (`railway.json`)** is not used: Railway deprecated it, services created
  after 2026-08-28 cannot use it, and Railway stops reading existing files on 2026-12-01.
- **Field support**: the reference documents the fields above except the cron schedule, the
  draining and overlap times, and the size limits, which the package's types accept. The cron
  schedule goes in the file if `railway config plan` shows it, and in the dashboard otherwise; the
  timings are variables (R7), which the file always holds.

**Rationale**: A plan-then-apply workflow is the preview FR-025 asks for, and a file in the
repository is the record FR-016 asks for. Keeping secrets out of the file and the Railway token out
of CI follows FR-008 and FR-025.

**Alternatives considered**:

- **The dashboard with a written list of settings**: no diff for networking, and a list drifts.
- **A community Terraform provider**: not Railway's own, and a second tool for the same records.
- **Applying from CI after merge**: automatic, but a Railway token in GitHub.

## R9. Logs (FR-012)

**Decision**:

- Railway collects every service's standard output and error. Its Log Explorer shows all services
  of an environment in one view for 7 days on Hobby. It parses single-line JSON: `message` is the
  text, `level` sets the severity, and every other field is filterable, for example
  `@request_id:<id>`, `@job_id:<id>`, `@run_id:<id>`, or `@snapshot_id:<id>`. The existing
  `JsonFormatter` already writes `level` and `message`.
- The structured lines of the earlier design stay: `request` (method, route template, status,
  `duration_ms`, request ID) from an API middleware, and `job_finished` (kind, outcome, attempt,
  `duration_ms`, job ID). Provider adapters log a warning with a `provider` field before each raised
  error. The allow-list of structured fields still keeps free text out.
- The worker's reporter adds `worker_status` every minute (R10).
- Railway's edge also keeps HTTP logs with status and duration, filterable with `@httpStatus` and
  `@totalDuration`. Whether they include the query string, where the OAuth callback carries
  `code` and `state`, is checked in the quickstart. The application's own logs never include it.
  If the edge does log it, the risk is accepted: the code is single-use, expires within minutes,
  is exchanged before the log is read, and is useless without the App's client secret.
- The operations guide lists the filters to use.

**Rationale**: The platform collects logs with no agent or credentials, and its JSON parsing reads
the fields the application already writes.

**Alternatives considered**: a third-party log service (another account that receives logs); a
log drain (a Pro feature).

## R10. Metrics and alerts (FR-013, FR-014, ADR 0016)

**Decision**:

- **Metrics** (FR-013): Railway's service metrics (CPU, memory, disk, and network per service, up
  to 30 days) cover resource use, including `db`'s volume. Request counts, errors, and latency come
  from Railway's HTTP metrics (`railway metrics --http`) and the API's `request` lines. Job
  durations, retries, and failures by kind come from `job_finished` lines; provider errors from the
  adapters' warnings; and the queue from `worker_status` lines (queued jobs and the oldest runnable
  job's age), which the worker writes every 60 seconds. There is no dashboard of our own.
- **What the platform alerts on**:
  - **Failed releases**: Railway's notification rules email the developer for build failures,
    failed deployments (a failed pre-deploy command or readiness check), and crashes after the
    restart retries. The developer sets these event types to email for the project. Each is a
    single event, with no recovery notice.
  - **Spending**: Railway's usage limits: an email alert at $20 (80% of the $25 budget, SC-002) and
    a hard limit at $50, twice the budget, which stops the services rather than let spending run
    past it (spec edge case). Railway also warns at 75% and 90% of the hard limit.
- **What the platform cannot see** (the public address from outside, the worker's silence, waiting
  jobs, failing jobs, and missing backups): the API serves one status endpoint per condition
  (contracts/http-api.md, data model "Alert conditions"), and an external monitor checks them:
  - `GET /status/worker` reads `worker_heartbeats`: the worker's reporter thread upserts its row
    every 60 seconds next to its job loop, because an indexing job can run for 15 minutes.
  - `GET /status/jobs-waiting` reads the oldest runnable job with the queue's own
    `runnable_waiting`, so jobs held back by their workspace's one-at-a-time rule do not count.
  - `GET /status/jobs-failing` counts the jobs that failed in the last hour with a counted
    failure, using the new `jobs.error_permanent` column with the rule of the earlier `JobsFailed`
    metric.
  - `GET /status/backup` reads the newest row of `backups`.
- **External monitor**: Better Stack's free plan (10 monitors, checks every 3 minutes, email alerts
  with recovery notices, for personal projects). Six monitors, all on the public address:
  `/` (the web app) and `/readyz` (the API and the database through the web app), each alerting
  after it has failed for about 5 minutes (FR-014's public address condition), and the four status
  endpoints, each alerting on its first 503 because the endpoint already includes the duration.
  The monitor runs outside Railway, so an outage of the platform still alerts (spec edge case).
  Its settings are recorded in the operations guide (R8).
- **Timing** (SC-007): a status condition alerts within one check, about 3 minutes, of becoming
  true; the stopped-worker alert therefore arrives 5 to 8 minutes after the last heartbeat, and the
  backup alert within minutes of the 26-hour mark.
- **Removed**: the CloudWatch embedded metric format, `EMIT_METRICS`, and `METRICS_ENVIRONMENT`;
  the disk and certificate alerts, which the developer dropped (the platform manages the
  certificate, and an invalid one fails the external check like an outage).
- **Validation**: the fault tests in the quickstart stop `web`, stop `worker` with jobs queued,
  make three jobs exceed their deadline (a counted failure), fail a release's readiness check, and
  skip the backup. Integration tests cover each condition's query.

**Rationale**: Each condition becomes a query over tables the application already owns, tested
like any other code, and one free external service turns status codes into emails with recovery
notices. Nothing in the application needs credentials for a monitoring service.

**Alternatives considered**:

- **Heartbeat monitors** (the worker and the backup ping a monitoring URL): no new tables, but the
  ping URLs are credentials in two services, and the waiting and failing conditions would still
  need code that decides and reports them.
- **UptimeRobot's free plan**: 50 monitors, but checks every 5 minutes, which leaves no margin in
  SC-007's 10 minutes, and non-commercial use only.
- **Alerting from inside Railway** (a cron service that checks and sends email): it fails with the
  platform it watches, and needs an email provider.
- **A metrics stack** (Prometheus and Grafana as services): more services and memory than the
  pilot itself, for numbers the logs already hold.

## R11. Liveness, readiness, and the running version (FR-011, FR-019)

**Decision**: unchanged from the earlier design and already implemented, except where noted.

- `GET /healthz`: liveness; the process answers.
- `GET /readyz`: `SELECT 1` with a 2-second statement timeout; 200 or 503 `not_ready`. It checks
  neither GitHub nor the model providers (FR-011). It is `api`'s deployment healthcheck (R7) and an
  external monitor (R10).
- `GET /version`: `{"commit": ...}` from `CODEATLAS_RELEASE` or `RAILWAY_GIT_COMMIT_SHA` (R6).
- The web footer shows the short commit.

**Rationale**: as before, separating liveness from readiness keeps a database outage from looking
like a dead process.

**Alternatives considered**: a readiness check that includes the alert conditions (a backlog would
then block releases and restart the API).

## R12. Backups, restore, and recovery (FR-015 to FR-018, SC-008)

**Decision**:

- **Nightly backup**: the `backup` service is a Railway cron service, `30 3 * * *` (UTC). It runs
  `python -m codeatlas.ops backup` and exits; Railway skips a run while the previous one is still
  active. The command:
  1. opens a `REPEATABLE READ` transaction, exports its snapshot with `pg_export_snapshot()`, and
     runs `pg_dump -Fc --snapshot=<id>` into a temporary file while the transaction stays open;
  2. in the same transaction, counts the rows of every table in the `public` schema and reads the
     Alembic revision, so the counts describe the dumped data even while users write;
  3. checks the dump with `pg_restore --list`, and computes its size and SHA-256;
  4. builds `manifest.json` with the existing pure functions in `codeatlas.ops.backup`;
  5. uploads `backups/<UTC date>/codeatlas.dump`, then `manifest.json`, to the bucket;
  6. inserts the `backups` row (data model) only after both uploads; and
  7. deletes the objects and rows of backups older than `BACKUP_RETENTION_DAYS` (7).
  Any failure exits non-zero with no row, so `/status/backup` alerts after 26 hours.
- **Storage**: a Railway bucket, `backups`, private and encrypted at rest. Its endpoint is
  S3-compatible (`boto3`, with the endpoint, region, and addressing style the bucket's credentials
  give). Railway buckets have no lifecycle rules, versioning, or object lock, and one key pair per
  bucket with full access, so the backup command prunes old backups itself, and the backup service
  could also delete backups. Uploads cross the public network and count as egress (R19).
- **Restore**: `python -m codeatlas.ops restore <UTC date>`, run in the `api` service through
  `railway ssh` with the bucket's credentials exported for that session only (from the bucket's
  Credentials tab), so `api` holds them only while the developer restores. It downloads the dump
  and manifest, checks the SHA-256, runs `pg_restore --clean --if-exists --no-owner
  --exit-on-error` into `db`, and runs `verify-restore`, which compares the Alembic revision and
  every table's row count with the manifest. The guide stops the worker first (`railway down -s
  worker`) and redeploys it afterward.
- **Recovery exercise** (SC-008): the developer duplicates `production` into `drill`. The copy
  has the services, configuration, and non-secret variables, an empty volume, and a new domain;
  the developer sets the sealed secrets again, removes the worker's deployment so no background
  job acts on the restored data, waits for `api` to migrate the empty database, restores the latest
  backup, and adds `https://<drill domain>/auth/github/callback` as a second callback URL of the
  pilot App. They sign in on the drill, check their repositories, answers, and reviews, record the
  time from duplication to that check, and delete the environment and the callback URL.
- **Losses in the pilot**:
  - A crashed or restarted service returns by itself (R2).
  - A lost database volume: create a new volume for `db`, let `api` migrate the empty database,
    restore the latest backup, and redeploy the worker. Up to 24 hours of changes are lost
    (FR-031). The guide tells the developer to inform pilot users of the backup time, so they can
    disconnect repositories again; access losses are found again by the daily check (002 FR-012).
  - A lost environment or project: rebuild it with `railway config apply` (R8), set the secrets,
    reclaim or replace the domain (R3), and restore as above. If the bucket was lost with the
    project, so are the backups (FR-031).
- **After a restore**: the newest `backups` row is the one before the restored backup, so
  `/status/backup` may alert until the next nightly run; the guide says to expect it, or to run the
  backup once by hand.
- **Disclosure** (FR-018): `ExternalProcessingAcceptance` and the README's data section keep saying
  that deleted data can remain in encrypted backups for up to 10 days: purged within 24 hours
  (001 FR-035), then held by backups that the nightly run deletes once they are more than 7 days
  old.

**Rationale**: A logical dump is portable to any PostgreSQL 17, can be verified by counting rows,
and restores into a new environment, which Railway's own volume backups (Pro) cannot. Doing the
whole backup in Python keeps the snapshot handling, the manifest, and the pruning in tested code.

**Alternatives considered**:

- **Railway volume backups** (Pro plan): no code, but they restore only into the same environment,
  cannot be verified by row counts, and are lost with the project.
- **A shell script with `psql` and the AWS CLI** (the earlier design): the snapshot needed `psql`'s
  `\!` and `:SHELL_ERROR`, and the AWS CLI would join the image.
- **An external bucket** (S3 or R2): backups would survive the loss of the Railway account, but
  another account and long-lived keys; the developer chose to keep everything on Railway, and the
  limitation is stated (FR-031).
- **Continuous WAL archiving**: minutes of data loss instead of a day, with another tool to run.

## R13. Pilot access list (FR-002 to FR-004, ADR 0013)

**Decision**: unchanged and already implemented: the `pilot_users` table, checked at sign-in
before anything is stored; denied users get `/?error=not_invited` and one denied `sign_in` audit
event; at most 10 users (`PILOT_USER_LIMIT`). The operator commands are the same, run through the
platform's SSH:
`railway ssh -s api -- python -m codeatlas.ops pilot-users add <login> [--note TEXT]`, and
likewise `remove`, `delete-data`, and `list`. No release is needed.

**Rationale and alternatives**: as recorded in ADR 0013. `railway ssh` replaces the host's Session
Manager; it authenticates with the developer's registered SSH key, and the command runs in the
`api` container with its variables.

## R14. Pilot-wide daily limits (FR-005, SC-003, ADR 0013)

**Decision**: unchanged and already implemented: `pilot_usage_counters` with guarded upserts in the
same transaction as the workspace counter; `429 pilot_limit_reached` with the reset time; 30
questions and 15 reviews a day. The worst case is $14.76 a day at the Gemini prices from
2027-01-01, with one billed attempt per request (001 R11, 003 R7), and about $44 if every retry were
fully billed:

| Request | Per call | Per request (2 calls) | Daily limit | Daily worst case |
| --- | --- | --- | --- | --- |
| Question (about 20,000 input, 16,000 output tokens) | $0.15 | $0.30 | 30 | $9.00 |
| Review (48,000 input, 16,000 output tokens) | $0.19 | $0.38 | 15 | $5.76 |
| **Total** | | | | **$14.76** |

Changing a limit is a variable change on `api` and `worker`, deployed from the staged changes.

## R15. Load test (FR-026, SC-011)

**Decision**:

- **Target**: a `loadtest` environment duplicated from `production`: the same services, sizes,
  and commit, an empty database, and its own domain. Its variables are overridden before the first
  deployment: `CODEATLAS_ENV=development`, `CODEATLAS_FAKE_EXTERNALS=1`, raised limits,
  `RATE_LIMIT_PER_MINUTE=0`, a new `TOKEN_ENCRYPTION_KEY`, and no other secrets. The `backup`
  service's cron schedule is removed in this environment. The environment is deleted after the
  run.
- **Driver**: the existing `evals/perf_check.py` (001 SC-007's categories, fake sign-in included),
  through the existing `load-test.yml` workflow from a GitHub-hosted runner, at 10 concurrent
  users, then 20, 40, and 80, stopping at the first level that misses a target or returns errors.
  The workflow measures its network baseline with connections to `/version` instead of `/healthz`,
  which is no longer public; `/version` reads no database, so it still isolates network time.
- **Resources**: each service's CPU and memory during each level come from Railway's metrics for
  the environment; HTTP latency at the edge from `railway metrics --http`.
- **Report**: `docs/reports/load-test.md`: the steps to rerun it, the service sizes, the
  configuration, the commit, the duration, the network baseline, each level's p95 latency and error
  rate by category, the highest level that meets every target (or "at least 80"), and peak CPU and
  memory per service. The worker's memory peak decides whether its 2 GB limit can come down.

**Rationale**: `perf_check` already encodes the request mix that 001 SC-007 was measured with. A
copied environment has the pilot's configuration without its data or secrets.

**Alternatives considered**: k6 or Locust (a second script to keep in sync); running against the
pilot itself (its data, limits, and users would be affected).

## R16. Evaluation (FR-027, FR-028, SC-012)

**Decision**: unchanged from the earlier design and independent of hosting; the runner
`evals/run_bench_eval.py` is implemented.

- **Benchmark**: Code Review Bench (`withmartian/code-review-benchmark`, MIT license), pinned to a
  commit recorded in the report (`e616e849755441da38f18bf3adba2c9583b03803` was read while
  planning): the 10 Sentry (Python) and 10 Cal.com (TypeScript) pull requests of its offline set,
  with 36 and 41 golden comments written and verified by people. Four Sentry items carry data
  warnings, reported with and without them.
- **Runner**: resolves each pull request through GitHub's REST API, reviews it with the real
  Gemini model as `run_review_eval.py` does, raises the repository size limits for its own process
  (the pilot keeps the defaults), and exports one candidate per risk for the benchmark's judge.
- **Judge**: the benchmark's deduplication and judge steps with Claude Opus 4.5, then GPT-5.2 if
  needed, the judges behind its published results. If neither is served, the report re-judges the
  compared tools' published candidates with a current model and says so. The developer checks the
  providers' deprecation pages before the run.
- **Comparison**: precision and recall summed over the same 20 golden comments' pull requests,
  next to the published tools judged on the same labels; tools judged on older labels go in a
  separate table. The sums correspond to the benchmark's "All" profile.
- **Project sets** (FR-028): `run_qa_eval.py` on `qa_v1.jsonl` and `run_review_eval.py` on
  `review_v2.jsonl` with the real providers, labeled "measured on a set no person has reviewed yet"
  and "measured on a set reviewed only by a model". The human audits of 001 SC-005 and 003 SC-004
  are deferred and listed.
- **Report**: `docs/reports/evaluation.md`, with denominators, per-item failures, model cost and
  latency per item, and the commit, set versions, and SHA-256 of each set (SC-012).
- **Caveats in the report**: models may have seen the benchmark's repositories; the judge is a
  model; CodeAtlas reports risks only, so missed style findings count against recall; the
  benchmark's extraction step was skipped for CodeAtlas; the data warnings; and the size limits.

**Rationale and alternatives**: as recorded before: labels written by people, and other tools'
published results on the same pull requests, give the numbers a reference point; posting reviews
to GitHub forks, re-splitting risks with a model, or a judge of our own were rejected.

## R17. Documentation (FR-029 to FR-031)

**Decision**:

- **README**: a GIF of at most 60 seconds at the top (connect a repository, ask where something is
  implemented, open the cited code, review a pull request), recorded from the pilot and stored as
  `docs/assets/demo.gif` under 10 MB; a Mermaid architecture diagram of the Railway services, the
  private network, the bucket, GitHub, the model providers, and the external monitor; "Known
  limitations" (FR-031); the data section with the backup retention; and links to both reports and
  the operations guide. The "Deployment" section is rewritten for Railway.
- **Operations guide** `docs/operations.md`, rewritten: one-time setup (Railway account and CLI,
  SSH key, the project from `.railway/railway.ts`, secrets, the domain, the pilot GitHub App,
  notification rules, usage limits, the monitors), releases and the schema rule, rolling back,
  rehearsing a failed release, temporary settings, pilot users and deleting a removed user's data,
  replacing each credential, logs and metrics, backups and restoring, the recovery exercise,
  losing the database or the environment, the load test, fault tests, and tearing everything down
  (FR-030).

**Rationale**: a GIF plays inline on GitHub; Mermaid keeps the diagram reviewable as text.

## R18. Tests and checks

**Decision**:

- **Unit tests**:
  - settings: `GITHUB_APP_PRIVATE_KEY` content versus path, `RAILWAY_GIT_COMMIT_SHA` as the
    release, `CLIENT_ADDRESS_HEADER`, the new thresholds, and the backup settings named when
    missing;
  - the rate limiter's client address: the configured header, its absence, and no header
    configured;
  - the migration decision: upgrade, skip when the database is ahead, and what counts as ahead,
    from plain revision sets;
  - the backup pruning choice (which dates are older than the retention) and the manifest
    (existing);
  - the status conditions' thresholds, from plain values.
- **Integration tests** against PostgreSQL:
  - each `/status/*` endpoint: fresh and stale heartbeats; no runnable job, a recent one, an old
    one, and an old one held back by its workspace; counted and uncounted failures inside and
    outside the hour; no backup, a recent one, and an old one; 503 when the database is down;
  - the worker's reporter upserts its heartbeat and prunes old rows; a failed job records
    `error_permanent`;
  - `ops backup` end to end with an in-memory object store standing in for the bucket, and
    `ops restore` of its output into a second database, verified by `verify-restore` (both need
    `pg_dump` 17, which the CI job installs);
  - `ops migrate` on an empty database, a current one, and one ahead of the code; and the worker's
    schema wait.
- **Existing tests**: the access list, the pilot-wide limits, readiness, logging, and the operator
  commands stay. The EMF tests (`test_metrics.py`, `test_worker_metrics.py`) are replaced by the
  status and reporter tests above.
- **End-to-end** (Playwright, fake mode): unchanged; the shared stack keeps the access list off.
- **CI**:
  - removes the `infra` job (Terraform, shellcheck, bats, Compose, Caddy, and CloudWatch agent
    checks) and `release.yml`;
  - adds the `schema` job on pull requests (R7);
  - installs `postgresql-client-17` from the PostgreSQL apt repository in the backend job for the
    backup tests;
  - keeps the production image builds of both Dockerfiles, which Railway builds the same way.
- **Operational checks**, run by the developer and recorded in the quickstart: the fault tests
  (SC-007), the recovery exercise (SC-008), five releases with the probe and a failed-readiness
  rehearsal and rollback (SC-009, SC-010), the public-surface check (SC-005), and the credential
  scan (SC-006).

## R19. Monthly cost (SC-002)

Railway's Hobby plan is $5 a month and includes $5 of usage; usage beyond it is billed at $20 per
vCPU-month, $10 per GB-month of memory, $0.15 per GB-month of volume, $0.015 per GB-month of bucket
storage, and $0.05 per GB of egress. Builds are free. The estimate assumes the pilot's light load:

| Item | Assumption | Monthly |
| --- | --- | --- |
| Memory | `web` 0.15 GB, `api` 0.25 GB, `worker` 0.3 GB, `db` 0.25 GB on average | about $9.50 |
| CPU | 0.1 vCPU on average across services | about $2.00 |
| `db` volume | 2 GB | $0.30 |
| Bucket | 7 dumps of up to 0.5 GB, rounded up to whole GB-months | about $0.06 |
| Egress | nightly uploads (about 15 GB) and pages | about $1.00 |
| `backup` runs | a few minutes a night | under $0.10 |
| **Usage** | | **about $13** |
| **Bill** | $5 plan, which covers the first $5 of usage, plus the rest | **about $13** |

The budget is $25 (SC-002), with an email alert at $20 and a hard limit at $50 (R10). The external
monitor is free. Temporary environments cost cents per hour while they exist. Excluded, as in
SC-002: the model providers.

## R20. Leaving the AWS design

**Decision**:

- **Removed from the repository** (still in the history at `c0b0e22`): `infra/`, the AWS parts of
  `deploy/` (`compose.yml`, `Caddyfile`, `release.sh`, `render-config.sh`, `put-secrets.sh`,
  `backup.sh`, `restore.sh`, `check-certificate.sh`, `cloudwatch-agent.json`, `systemd/`, and the
  `bats` tests in `tests/`), `.github/workflows/release.yml`, CI's `infra` job, and the Terraform
  entries in `.gitignore`. `deploy/` is removed entirely; nothing in it applies to Railway.
- **Superseded records**: ADR 0010 by ADR 0014, ADR 0011 by ADR 0015, and ADR 0012 by ADR 0016.
  Each keeps its text, with its status changed and a pointer to its successor. ADRs 0002 and 0003
  note that they were revisited again and kept.
- **Kept from the earlier implementation**: everything in the application (the access list, the
  limits, the rate limiter, readiness and version, structured logs, the operator commands, the
  manifest and verification, the benchmark runner, the load test workflow), the Dockerfiles, and
  the migration `0004`.
- **The AWS account**: the earlier tasks that create resources (T003, T038, and later) never ran,
  so nothing needs tearing down. If the developer created anything by hand, the old operations
  guide's teardown in the history applies.

**Rationale**: Code that no environment runs would rot and mislead; the history and the superseded
ADRs keep the design and its reasoning.

**Alternatives considered**: keeping `infra/` as an alternative deployment (two deployments to
keep working, against the simplicity rule).
