# Implementation Plan: Pilot Deployment

**Branch**: `004-pilot-deployment` | **Date**: 2026-10-07; replanned 2026-10-12 for Railway |
**Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/004-pilot-deployment/spec.md`

## Summary

CodeAtlas runs as a pilot on the public internet for at most 10 invited GitHub users, on Railway at
the address it provides. Every change merged into `main` is released once CI passes, with
migrations first and traffic moved only after a readiness check. The developer learns about
failures by email and can rebuild the service from the recorded configuration and a backup. The
deployed system is measured with a load test at the pilot's service sizes and an evaluation with
the real model providers, including a public review benchmark.

The earlier plan for one AWS host (ADRs 0010 to 0012) is superseded; its code stays in the history
at `c0b0e22` (research R20). The application work it produced (the access list, the pilot-wide
limits, the rate limiter, readiness and version, structured logs, the operator commands, the
backup manifest, the benchmark runner, and the load test workflow) carries over.

Technical approach:

- **Hosting** (ADR 0014): one Railway project on the Hobby plan, in US East, with five services
  built from the repository: `web` (the only public one, at its `*.up.railway.app` domain), `api`,
  `worker`, a `backup` cron service, and `db` (`pgvector/pgvector:pg17` with a volume), plus a
  `backups` bucket. Services talk over the private network; the web app forwards the API's paths.
  About $13 a month (research R19).
- **Configuration** (ADR 0014): Railway's infrastructure as code in `.railway/railway.ts`, previewed
  with `railway config plan` and applied by the developer; secrets are sealed variables kept out of
  the file. Reference variables derive each environment's origin and database URL.
- **Releases** (ADR 0015): Railway deploys `main` after every GitHub check passes ("Wait for CI",
  on from the first deployment). `api`'s pre-deploy command migrates under an advisory lock and
  skips when the database is ahead, so rollbacks work; the other services' pre-deploy gates wait
  until `api` reports their commit, so a failed migration or readiness check stops the whole
  release; healthchecks gate traffic; rollback is one action per service, `api` first, within the
  platform's 72-hour image window. A CI job lists every schema change on pull requests. No
  platform credentials in CI.
- **Monitoring** (ADR 0016): Railway's logs (JSON fields searchable), metrics, deploy-failure
  emails, and usage limits. What the platform cannot see is served by four `/status/*` endpoints
  backed by a worker heartbeat table, the queue, a new `jobs.error_permanent` column, and a
  `backups` table, checked every 3 minutes by Better Stack's free monitors, which also check `/`
  and `/readyz` from outside the platform.
- **Backups**: `python -m codeatlas.ops backup` nightly: a snapshot-consistent `pg_dump` with row
  counts, a manifest, upload to the bucket, a `backups` row, and 7-day pruning; `ops restore` in
  the `api` container verifies the result. A duplicated `drill` environment rehearses recovery.
- **Measurement**: `perf_check` from the existing workflow against a duplicated `loadtest`
  environment; the evaluation is unchanged.

## Technical Context

**Language/Version**: unchanged for the application: Python 3.13 (API, worker, backup); TypeScript
5.9 on Node.js 24 LTS (frontend). New: TypeScript for Railway's infrastructure as code
(`.railway/railway.ts`).

**Primary Dependencies**:

- Backend: one new Python dependency, `boto3`, for the S3-compatible bucket (research R12); the
  image gains Debian's `postgresql-client-17`.
- Platform: Railway (services, private network, volume, bucket, cron, logs, metrics,
  notifications, usage limits) and the Railway CLI on the developer's machine; Better Stack's free
  uptime monitors.
- Evaluation only, outside the backend's dependencies: Code Review Bench's pipeline at a pinned
  commit, in its own environment under `evals/out/bench/`.

**Storage**: PostgreSQL 17 with pgvector in the `db` service, on a Railway volume (5 GB cap on
Hobby). Migration `0004_pilot_access` (done) and new `0005_operations_status`: `worker_heartbeats`,
`backups`, and `jobs.error_permanent`. The `backups` bucket holds nightly dumps and manifests for 7
days.

**Testing**:

- pytest unit tests for the new settings, the client address, the migration decision, the
  release gate, backup keys and pruning, the status thresholds, and the evaluations' per-item
  cost; Node's built-in test runner for the web app's gate script.
- Integration tests against PostgreSQL for the four status endpoints, the worker's reporter,
  `error_permanent`, `ops migrate`, and `ops backup` with `ops restore` end to end against an
  in-memory object store, including same-day runs, pruning after a failed dump, and restoring an
  older revision (needs `pg_dump` 17).
- Existing unit, integration, and Playwright suites unchanged, except the EMF tests, which the
  status tests replace, and the logging test that ran `backup-manifest`.
- CI: the backend job installs `postgresql-client-17`; the frontend job runs `node --test`; a
  `schema` job reports migrations on pull requests; a check limits the Dockerfiles' build
  arguments; the infrastructure job and the release workflow are removed; both production images
  are still built.
- Operational validation by the developer: fault tests, the recovery exercise, releases with a
  probe, a failed-readiness rehearsal and rollbacks, the public-surface check, and a credential
  scan (quickstart).

**Target Platform**: Linux containers on Railway (US East); current desktop browsers.

**Project Type**: Web application (frontend, HTTP API, background worker) plus platform
configuration.

**Performance Goals**:

- 001 SC-007's targets at the pilot's service sizes with 10 concurrent users, and the highest
  concurrency that still meets them (SC-011).
- A release serves within 15 minutes of its merge (CI takes about 5 minutes, builds a few more),
  with at most 60 seconds of interruption (SC-009); a rollback serves within 5 minutes (SC-010).
- Recovery within 2 hours (SC-008); alerts within 10 minutes of a condition (SC-007).

**Constraints**:

- Hosting at most $25 a month (SC-002); model spend at most $15 a day in the worst case (SC-003).
- Only `web` has a public address (FR-007); no platform credentials in the repository or CI
  (FR-025); secrets only as sealed variables on the services that use them (FR-008).
- Every write to the platform, GitHub settings, the monitors, and the pilot's data is a developer
  action after a preview: `railway config plan`, the staged-changes diff, a merged pull request, or
  a drafted command.
- Logs and backups kept 7 days; at most 24 hours of data loss; a 5 GB database volume.

**Scale/Scope**:

- At most 10 pilot users, with 001 and 003's per-workspace limits, and 30 questions and 15 reviews
  a day across the pilot.
- One always-on environment and two temporary copies; 5 services and a bucket per environment.
- 1 migration, 1 new route module with 4 endpoints, 4 new operator commands, 1 infrastructure file,
  1 new CI job, 3 ADRs, and 2 reports.

## Project Rules Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Rule | Gate for this plan | Before research | After design |
| --- | --- | --- | --- |
| English only | All artifacts, ADRs, code, and commit messages in English | Pass | Pass: every artifact is in English |
| Simplicity | Every new tool, service, table, endpoint, and dependency serves a current requirement | Pass, with the design document's ECS, RDS, SQS, staging, and tracing flagged for review | Pass: a managed platform instead of a host and Terraform; no staging, SQS, tracing, metrics stack, or operations UI; two small tables and one column for the alert conditions; one dependency (`boto3`) for the bucket; the AWS code is removed rather than kept as a second deployment. Each tool maps to a requirement below, and each deferral has a revisit condition (research deviations table) |
| Tested core | Core logic has automated tests; a story is done only when its tests pass | Pass | Pass: R18 covers the settings, client address, migration decision, status conditions and endpoints, reporter, both release gates, evaluation costs, and backup and restore end to end; the platform's behavior is validated by recorded exercises (quickstart) |
| Recorded decisions | Costly-to-reverse choices have ADRs | Pending | Pass: ADR 0014 (Railway, configuration, backups, the `boto3` dependency), ADR 0015 (releases on merge), ADR 0016 (status endpoints and the external monitor, with the schema change); ADRs 0010 to 0012 are marked superseded by them; ADRs 0002 and 0003 record that they were revisited again and kept; ADR 0013 is unchanged |
| Human in the loop | No external write without a draft and confirmation | Pass | Pass with two recorded deviations (Complexity Tracking): releases follow merges with no separate approval, and scheduled writes (the nightly backup, heartbeats, monitor checks, alert emails) run without a per-run confirmation. Configuration changes apply only after `railway config plan` or the staged diff; secrets, domains, the GitHub App, monitors, and operator commands are developer actions; the evaluation never posts to GitHub |
| Repository hygiene | No absolute local paths, secrets, or personal notes committed | Pass | Pass: secrets are sealed variables declared with `preserve()` and never in `.railway/railway.ts`; no token in CI; bucket keys reach only the `backup` service, and `api` only for a restore session |

Tool and service to need map:

| Tool or service | Need |
| --- | --- |
| Railway services, builds, private network | Running the four processes from the repository with no host (FR-001, FR-007, FR-017) |
| Railway domain and edge | HTTPS address with automatic renewal and redirects, no domain to buy (FR-001) |
| Railway volume | The database's data (FR-015, FR-016) |
| Railway bucket and cron | Nightly backups off the database's storage (FR-015) |
| Railway infrastructure as code and CLI | A recorded, previewed configuration (FR-016, FR-025); operator commands through `railway ssh` (FR-004) |
| Railway GitHub integration with "Wait for CI" | Releases from `main` only after checks pass, with no credentials in CI (FR-019, FR-020, FR-025) |
| Railway pre-deploy command and healthchecks | Migrations first and readiness before traffic (FR-021, FR-022) |
| Railway logs, metrics, notifications, usage limits | Central logs, resource views, release-failure and spending alerts (FR-012 to FR-014) |
| Better Stack (free) | The external check and the conditions the platform cannot see, with recovery notices (FR-014) |
| `boto3` | The bucket's S3-compatible API for upload, listing, download, and pruning (FR-015) |
| `postgresql-client-17` in the backend image | `pg_dump` and `pg_restore` matching the server (FR-015, SC-008) |
| Code Review Bench pipeline (evaluation only) | Scoring comparable with published results (FR-027) |

## Project Structure

### Documentation (this feature)

```text
specs/004-pilot-deployment/
├── plan.md                 # This file
├── research.md             # Phase 0: decisions, rationale, alternatives
├── data-model.md           # Phase 1: tables, conditions, manifest, settings
├── quickstart.md           # Phase 1: prerequisites and validation scenarios
├── contracts/
│   ├── http-api.md         # Phase 1: changes to the 001 to 003 HTTP API
│   └── operations.md       # Phase 1: environments, services, variables, commands, releases, monitors
├── checklists/
│   └── requirements.md     # Spec quality checklist
└── tasks.md                # Phase 2 (/speckit-tasks)

docs/decisions/
├── 0002-postgresql-as-the-only-data-store.md   # amended: revisited for Railway and kept
├── 0003-postgresql-job-queue.md                # amended: revisited for Railway and kept
├── 0010-single-host-aws-deployment.md          # superseded by 0014
├── 0011-terraform-and-approved-releases.md     # superseded by 0015
├── 0012-cloudwatch-monitoring.md               # superseded by 0016
├── 0013-pilot-access-list-and-limits.md        # unchanged
├── 0014-railway-hosting.md                     # new
├── 0015-releases-on-merge.md                   # new
└── 0016-status-endpoints-and-external-monitor.md   # new
```

### Source Code (repository root)

New, changed, and removed files. Everything else is unchanged; tasks.md names the task for each.

```text
.railway/
├── railway.ts                       # new: services, db image and volume, bucket, variables (secrets preserved), commands, healthchecks, cron
└── package.json, package-lock.json  # new: the pinned `railway` package the file imports

backend/
├── Dockerfile                       # + postgresql-client-17; release read from RAILWAY_GIT_COMMIT_SHA (build argument kept for local builds)
├── pyproject.toml, uv.lock          # + boto3
├── alembic/versions/
│   └── 0005_operations_status.py    # new: worker_heartbeats, backups, jobs.error_permanent
├── src/codeatlas/
│   ├── config.py                    # GITHUB_APP_PRIVATE_KEY, CLIENT_ADDRESS_HEADER, thresholds, BACKUP_*, release alias; - EMIT_METRICS, METRICS_ENVIRONMENT
│   ├── status.py                    # replaces metrics.py: the worker's reporter (heartbeat, worker_status line) and the four conditions
│   ├── models.py                    # WorkerHeartbeat, Backup, Job.error_permanent
│   ├── api/
│   │   ├── app.py                   # client address from CLIENT_ADDRESS_HEADER; /status/* rate-limited; status router
│   │   └── routes/status.py         # new: GET /status/{condition}
│   ├── github/client.py             # signs with the key content or the key file
│   ├── jobs/
│   │   ├── queue.py                 # records error_permanent; no metric call
│   │   └── worker.py                # always starts the reporter
│   └── ops/
│       ├── __main__.py              # + migrate, wait-for-api, backup, restore; - backup-manifest
│       ├── backup.py                # pure: manifest and comparison (existing); keys and pruning choice (new)
│       ├── dump.py                  # new: snapshot dump with counts, pg_restore list, restore into an empty schema
│       ├── storage.py               # new: the bucket through boto3, behind a small protocol that tests replace
│       └── release.py               # new: the migration decision, advisory lock, and release gate
└── tests/
    ├── unit/                        # new: test_status_conditions.py, test_release.py, test_backup_pruning.py,
    │                                #      test_client_address.py, test_eval_pricing.py; extended: test_config.py,
    │                                #      test_production_settings.py, test_github_client_auth.py; removed: test_metrics.py
    └── integration/                 # new: test_status_endpoints.py, test_worker_reporter.py, test_backup_restore.py,
                                     #      test_ops_migrate.py; extended: test_ops_commands.py, test_logging.py;
                                     #      removed: test_worker_metrics.py

backend/evals/
├── pricing.py                       # new: the price function, moved from run_bench_eval.py
├── run_qa_eval.py                   # per-question tokens, latency, and cost
└── run_review_eval.py               # per-item cost

frontend/
├── Dockerfile                       # build stage: ARG API_ORIGIN, ARG RAILWAY_GIT_COMMIT_SHA; production stage copies the gate script
├── next.config.ts                   # rewrites + /webhooks, /status, /readyz, /version; Strict-Transport-Security header
└── scripts/
    ├── wait-for-api.mjs             # new: the web app's release gate
    └── wait-for-api.test.mjs        # new: node --test

.github/workflows/
├── ci.yml                           # - infra job; + schema job; build-argument check; postgresql-client-17; node --test
├── release.yml                      # removed
└── load-test.yml                    # network baseline against /version

infra/                               # removed (history: c0b0e22)
deploy/                              # removed (history: c0b0e22)

docs/
├── operations.md                    # rewritten for Railway
├── reports/load-test.md, evaluation.md   # new
└── assets/demo.gif                  # new

README.md                            # Deployment, architecture diagram, known limitations, data section
.env.example                         # new thresholds and settings; - EMIT_METRICS
.gitignore                           # - Terraform entries
```

**Structure Decision**: The web application layout of 001 (ADR 0001), plus `.railway/` for the
platform's configuration. Application changes stay inside existing packages; `codeatlas.ops` gains
the platform-facing commands, with pure parts (`backup.py`, the migration decision) that unit tests
cover without a database, and `storage.py` as the only code that talks to the bucket. The only
other external boundaries remain `GitHubGateway` and the model adapters.

### Carried over from the earlier implementation

The earlier task list completed every task except the developer actions (T003, T038, T057, T062,
T064, T068, T071, T075, T077) and two documentation tasks (T072, T076). Of that work, these parts
are kept: migration `0004`, the access list and operator commands, the
pilot-wide limits, the rate limiter, production settings validation without values in errors,
`/readyz` and `/version`, structured `request` and `job_finished` lines with the field allow-list,
provider warnings, the backup manifest and `verify-restore`, the invitation page and limit messages,
the footer commit, the disclosure sentence, the standalone web image, the benchmark runner, and the
load test workflow. The metric emission (EMF), the host scripts, Terraform, and the release
workflow are replaced or removed (research R20).

## Complexity Tracking

| Violation | Why needed | Simpler alternative rejected because |
| --- | --- | --- |
| Releases write to the platform after a merge, with no separate confirmation of the release. The rule asks for a draft and an explicit confirmation before an external write | The developer chose releases without approval (spec Input, FR-020). The draft is the pull request: its diff, its checks, and the `schema` job's report; the confirmation is the developer's merge, which covers exactly that commit. Railway deploys only `main` and only after every check passes (FR-019) | An approval step per release needs a release workflow holding platform credentials in CI, which FR-025 forbids, and was declined by the developer. Direct pushes to `main` also release, so the operations guide says to merge through pull requests |
| Scheduled writes run without a confirmation each time: the nightly backup upload, pruning, and `backups` row; the worker's heartbeats; the monitors' checks and alert emails | FR-014 and FR-015 require daily backups and continuous alerts; nobody can confirm each one. The developer confirms them once, in `railway config plan` and the monitor setup, and each write does only what that preview showed: one bucket, one table, and one email address | A confirmation per backup or per alert defeats the purpose of both; leaving them out violates FR-014 and FR-015 |

Every design-document mechanism left out is a simplification, recorded with its revisit condition
in research.md.
