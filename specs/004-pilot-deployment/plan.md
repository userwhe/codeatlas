# Implementation Plan: Pilot Deployment

**Branch**: `004-pilot-deployment` | **Date**: 2026-10-07 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/004-pilot-deployment/spec.md`

## Summary

CodeAtlas runs as a pilot on the public internet for at most 10 invited GitHub users. The developer
releases changes after approving a summary, learns about failures by email, and can rebuild the
service from code and a backup. The deployed system is measured with a load test on the pilot's
hardware and an evaluation with the real model providers, including a public review benchmark.

Technical approach:

- **Hosting** (ADR 0010): one `t4g.medium` EC2 instance in `us-east-1` runs the existing Compose
  services behind Caddy, which provides HTTPS with Let's Encrypt. PostgreSQL keeps its data on a
  separate EBS volume. The host has no SSH; administration uses SSM. About $34 a month.
- **Infrastructure and releases** (ADR 0011): Terraform defines a shared module and one stack
  module per environment (`pilot`, `drill`, `loadtest`). After CI passes on a push to this
  repository's `main`, GitHub Actions builds Arm images as workflow artifacts, posts a release
  summary, and waits for the developer's approval. Only then does it push the images to ECR and
  run a host script that migrates, starts the new images, checks them, and rolls back on failure.
  One OIDC role, trusted only for the `pilot` environment, replaces stored AWS keys.
- **Monitoring** (ADR 0012): container logs go to CloudWatch through Docker's `awslogs` driver.
  Nine custom metrics and two Route 53 health checks (`/readyz` and `/`) feed ten alarms, emailed
  through SNS with recovery notices. The dashboard computes everything else from the JSON logs.
- **Backups**: a nightly `pg_dump` to S3 with row counts taken in the dump's own snapshot, kept 7
  days and never overwritable by the host; a recovery exercise rebuilds a drill environment and
  restores into it.
- **Access and spend** (ADR 0013): a `pilot_users` table checked at sign-in before anything is
  stored, managed by operator commands; pilot-wide daily limits of 30 questions and 15 reviews cap
  model spend near $15 a day; the API rate-limits sign-in and webhook paths per address.
- **Hardening found during planning**: settings errors currently print every input value,
  including secrets; `hide_input_in_errors` and a full production check fix that (research R5).
- **Measurement**: `perf_check` runs from a GitHub Actions workflow against a temporary environment
  at increasing concurrency. The evaluation adds Code Review Bench's 20 Python and TypeScript pull
  requests, scored by the benchmark's own judge, next to other tools' published results.

## Technical Context

**Language/Version**: unchanged for the application: Python 3.13 (API and worker); TypeScript 5.9
on Node.js 24 LTS (frontend). New: Terraform HCL (Terraform 1.11 or later, AWS provider 6) and
Bash for host scripts.

**Primary Dependencies**: no new Python or npm dependency.

- Hosting: Docker Engine with the Compose plugin, Caddy 2.10, the CloudWatch agent, and the AWS CLI
  v2 on Ubuntu 24.04.
- CI only: `bats` for the host script tests.
- AWS: EC2, EBS, ECR, S3, SSM (Parameter Store, Run Command, Session Manager), CloudWatch (Logs,
  metrics, alarms, dashboard), SNS, Route 53, Budgets, and IAM with GitHub's OIDC provider.
- Evaluation only, outside the backend's dependencies: Code Review Bench's pipeline at a pinned
  commit, in its own environment under `evals/out/bench/`.

**Storage**: PostgreSQL 17 with pgvector, in a container on the host, data on an encrypted EBS
volume. One migration, `0004_pilot_access`, adds `pilot_users` and `pilot_usage_counters` and three
audit actions. S3 holds nightly dumps (7 days) and release bundles.

**Testing**:

- pytest unit tests for the rate limiter, production settings validation, EMF lines, structured log
  fields, the backup manifest and restore comparison, and the benchmark export and comparison.
- Integration tests against PostgreSQL for the access list (no rows for uninvited users, session
  revocation, data deletion), pilot-wide limits under concurrency, readiness, and queue metrics.
- Playwright in fake mode: the invitation page and the pilot-wide limit message (API response
  intercepted).
- `bats` tests for `release.sh` and `render-config.sh` with stub commands: exit codes, rollback,
  state files, and required parameters.
- CI: production image builds, `terraform fmt` and `validate`, `shellcheck`, the `bats` tests,
  `docker compose config`, and `caddy validate`.
- Operational validation by the developer: fault tests, the recovery exercise, releases and a
  forced rollback, a port scan, and a credential scan (quickstart).

**Target Platform**: Linux containers on one `arm64` EC2 instance (Ubuntu 24.04) in `us-east-1`;
current desktop browsers.

**Project Type**: Web application (frontend, HTTP API, background worker) plus infrastructure code.

**Performance Goals**:

- 001 SC-007's targets on the pilot's hardware with 10 concurrent users, and the highest
  concurrency that still meets them (SC-011).
- A release serves the new version within 15 minutes of approval, with at most 60 seconds of
  interruption (SC-009); a failed check restores the previous version within 5 minutes (SC-010).
- Recovery from host loss within 2 hours (SC-008); alerts within 10 minutes of a fault (SC-007).

**Constraints**:

- Hosting at most $40 a month (SC-002); model spend at most $15 a day in the worst case (SC-003).
- Only ports 80 and 443 reachable (FR-007); no long-lived cloud keys in the repository or CI
  (FR-024); no secret values in Terraform state, images, or logs (FR-008, SC-006).
- Every write to AWS, GitHub settings, DNS, and the pilot's data is a developer action after a
  preview: `terraform plan`, the release summary, or a drafted command. A release writes nothing
  to AWS before its approval.
- Logs and backups kept 7 days; at most 24 hours of data loss.

**Scale/Scope**:

- At most 10 pilot users, with 001 and 003's per-workspace limits, and 30 questions and 15 reviews
  a day across the pilot.
- Three environments from one definition; the drill and load test environments exist for hours.
- About 20 Terraform resources per stack, 5 host scripts and 1 developer script, 2 new workflows,
  1 migration, 1 new operator command group, and 2 reports.

## Project Rules Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Rule | Gate for this plan | Before research | After design |
| --- | --- | --- | --- |
| English only | All artifacts, ADRs, code, and commit messages in English | Pass | Pass: every artifact is in English |
| Simplicity | Every new tool, service, table, and endpoint serves a current requirement | Pass, with the design document's ECS, RDS, SQS, staging, and tracing flagged for review | Pass: one host instead of managed services; no staging, SQS, tracing, or operations UI; nine custom metrics, everything else from log queries; an in-process rate limiter instead of a proxy module or WAF. Each tool maps to a requirement below, and each deferral has a revisit condition (research deviations table) |
| Tested core | Core logic has automated tests; a story is done only when its tests pass | Pass | Pass: R18 covers the access list, limits, rate limiter, settings validation, readiness, metrics, and restore verification; `bats` tests cover the release script's state transitions and the configuration check; the rest of the operational behavior is validated by recorded exercises (quickstart) |
| Recorded decisions | Costly-to-reverse choices have ADRs | Pending | Pass: ADR 0010 (hosting), ADR 0011 (Terraform and approved releases), ADR 0012 (monitoring), ADR 0013 (access list and limits, with the schema change); ADRs 0002 and 0003 record that they were revisited and kept; ADR 0007's accepted risk now names the rate limit |
| Human in the loop | No external write without a draft and confirmation | Pass | Pass: infrastructure applies only a reviewed saved plan; each release builds without cloud credentials, then waits for approval of its summary in a protected environment before pushing images or touching the host, and covers only that commit; only pushes to this repository's `main` start a release; secrets, the GitHub App, DNS, and operator commands are developer actions; the evaluation never posts to GitHub. Scheduled writes to the project's own account (the nightly backup upload, metrics, and alert emails) run without a per-run confirmation: the developer confirms them once, by approving the Terraform plan and the release that install them, and each does only what that preview shows. This is recorded as a justified deviation in Complexity Tracking |
| Repository hygiene | No absolute local paths, secrets, or personal notes committed | Pass | Pass: secret values go to Parameter Store by script, never to the repository or Terraform state; real `*.tfvars` files (alert email, domain, OIDC subject prefix) are ignored, and only examples are committed |

Tool and service to need map:

| Tool or service | Need |
| --- | --- |
| EC2, EBS, Elastic IP | The host, its data volume, and a stable address (FR-001, FR-016, FR-017) |
| Caddy | HTTPS with automatic renewal and redirects (FR-001) |
| Terraform | A recorded, previewed definition of every environment (FR-016, FR-025) |
| ECR | Release images labeled by commit, pulled without credentials (FR-019) |
| GitHub environments and OIDC | Approval before a release; short-lived credentials (FR-020, FR-024) |
| GitHub workflow artifacts | Images built before approval without writing to AWS (FR-020) |
| SSM Run Command and Session Manager | Releases and administration without inbound ports (FR-007) |
| SSM Parameter Store | Credentials outside the repository and images (FR-008) |
| S3 | Off-host encrypted backups and release bundles (FR-015) |
| CloudWatch Logs, metrics, alarms, dashboard; CloudWatch agent | Central logs, metrics, alerts (FR-012 to FR-014) |
| SNS | Alert and recovery emails (FR-014, FR-022) |
| Route 53 | The domain and the external check (FR-001, FR-014) |
| Budgets | The spending alert (FR-014) |
| `bats` (CI only) | Automated tests of the host scripts' state transitions (tested-core rule) |
| Code Review Bench pipeline (evaluation only) | Scoring comparable with published results (FR-027) |

## Project Structure

### Documentation (this feature)

```text
specs/004-pilot-deployment/
├── plan.md                 # This file
├── research.md             # Phase 0: decisions, rationale, alternatives
├── data-model.md           # Phase 1: new tables, audit actions, manifest, settings
├── quickstart.md           # Phase 1: prerequisites and validation scenarios
├── contracts/
│   ├── http-api.md         # Phase 1: changes to the 001 to 003 HTTP API
│   └── operations.md       # Phase 1: environments, parameters, commands, scripts, workflows, metrics
├── checklists/
│   └── requirements.md     # Spec quality checklist
└── tasks.md                # Phase 2 (/speckit-tasks)

docs/decisions/
├── 0002-postgresql-as-the-only-data-store.md   # amended: revisited and kept
├── 0003-postgresql-job-queue.md                # amended: revisited and kept
├── 0007-github-webhooks-for-change-notifications.md  # amended: rate limit
├── 0010-single-host-aws-deployment.md          # new
├── 0011-terraform-and-approved-releases.md     # new
├── 0012-cloudwatch-monitoring.md               # new
└── 0013-pilot-access-list-and-limits.md        # new
```

### Source Code (repository root)

New and changed files. Everything else is unchanged from 003. tasks.md names the task for each.

```text
infra/
├── shared/                          # new: ECR, S3 bucket, SNS topic, OIDC provider and release role, hosted zone, budget
│   ├── main.tf, iam.tf, variables.tf, outputs.tf, versions.tf
│   └── shared.tfvars.example, backend.hcl.example
└── stack/                           # new: one environment per state key
    ├── main.tf                      # instance, data volume, Elastic IP, security group, instance role
    ├── dns.tf, logs.tf
    ├── monitoring.tf                # alarms, dashboard, health checks (pilot only)
    ├── cloud-init.yaml.tftpl        # Docker, AWS CLI, CloudWatch agent, mount, swap, compose wrapper, timers
    ├── variables.tf, outputs.tf, versions.tf
    └── pilot.tfvars.example, drill.tfvars.example, loadtest.tfvars.example, backend.hcl.example

deploy/                              # new: copied to the host as the release bundle
├── compose.yml                      # caddy, web, api, worker, db, migrate; awslogs; restart policies
├── Caddyfile                        # routes, HSTS, log filters
├── release.sh                       # migrate, switch current, start, check, roll back
├── render-config.sh                 # SSM parameters and derived settings to runtime/
├── backup.sh, restore.sh, check-certificate.sh
├── put-secrets.sh                   # developer machine: writes SSM parameters
├── cloudwatch-agent.json            # memory and disk metrics, host log files
├── systemd/                         # codeatlas-backup and codeatlas-cert-check services and timers
└── tests/                           # bats tests with stub commands for every host script

.github/workflows/
├── ci.yml                           # + production image builds, terraform, shellcheck, bats, compose and Caddy checks
├── release.yml                      # new: build artifacts, summary, approved push and release, outage probe, alert
└── load-test.yml                    # new: perf_check per level, report artifact

backend/
├── Dockerfile                       # CODEATLAS_RELEASE build argument; fixture repositories for fake mode
├── alembic/versions/
│   └── 0004_pilot_access.py         # new: pilot_users, pilot_usage_counters, audit actions
├── src/codeatlas/
│   ├── config.py                    # production validation, hide_input_in_errors, new settings
│   ├── logging.py                   # allow-listed structured fields
│   ├── metrics.py                   # new: EMF lines and the reporter thread
│   ├── models.py                    # PilotUser, PilotUsageCounter, audit actions
│   ├── api/
│   │   ├── app.py                   # /readyz, /version, request log and rate-limit middleware
│   │   └── ratelimit.py             # new: per-address sliding window
│   ├── auth/
│   │   ├── access_list.py           # new: is_allowed, add, remove, delete_data
│   │   └── github_login.py          # access check before any write
│   ├── api/routes/auth.py           # not_invited redirect and denied audit event
│   ├── github/
│   │   ├── gateway.py, client.py    # get_user_by_login
│   │   └── fake.py                  # the same, for tests
│   ├── workspace/quotas.py          # pilot-wide reservation and refund
│   ├── providers/answer_model.py, embeddings.py   # a provider-field warning before each raised error
│   ├── jobs/
│   │   ├── queue.py                 # fail returns the final status; JobsFailed and job_finished where a job fails
│   │   └── worker.py                # starts the metrics reporter; job_finished for other outcomes
│   └── ops/                         # new: python -m codeatlas.ops
│       ├── __init__.py
│       ├── __main__.py              # pilot-users, backup-manifest, verify-restore
│       └── backup.py                # pure: manifest building and comparison
├── tests/
│   ├── conftest.py                  # rate limit off and generous pilot-wide limits for the suites
│   ├── unit/                        # new: test_ratelimit.py, test_production_settings.py, test_metrics.py,
│   │                                # test_backup_manifest.py, test_bench_eval.py, test_github_users_client.py,
│   │                                # test_perf_check.py; extended: test_config.py, test_logging_redaction.py,
│   │                                # test_fake_github.py, test_gemini_answer_model.py, test_voyage_embedder.py
│   └── integration/                 # new: test_pilot_schema.py, test_access_list.py, test_pilot_limits.py,
│                                    # test_readiness.py, test_worker_metrics.py, test_ops_commands.py;
│                                    # extended: test_logging.py
└── evals/
    ├── perf_check.py                # --json summary output
    ├── load_report.py               # new: load test levels to a Markdown table
    ├── run_bench_eval.py            # new: Code Review Bench runner, export, and comparison
    └── README.md                    # benchmark section; review-status labels

frontend/
├── Dockerfile                       # multi-stage: dev and production (standalone) targets
├── next.config.ts                   # output: "standalone"
├── src/
│   ├── app/page.tsx, app/sign-in.tsx # not_invited explanation
│   ├── app/layout.tsx               # footer with the release commit
│   ├── components/ExternalProcessingAcceptance.tsx   # backup retention sentence
│   └── lib/api/questions.ts, reviews.ts              # pilot_limit_reached messages
└── tests/e2e/
    └── pilot-access.spec.ts         # new

docs/
├── operations.md                    # new: operations guide
├── reports/
│   ├── load-test.md                 # new
│   └── evaluation.md                # new
└── assets/demo.gif                  # new

docker-compose.yml                   # web builds the dev target
.env.example                         # the new limits, with the pilot's defaults noted
.gitignore                           # .terraform/, *.tfstate*, infra/**/*.tfvars, plan files
README.md                            # demo, architecture diagram, limitations, data section, links
docs/design/codeatlas-v1.md          # superseded-in-part note for spec 004
```

**Structure Decision**: The web application layout of 001 (ADR 0001), plus two top-level
directories for operations.

- `infra/` holds the definition of the cloud environment; `deploy/` holds what runs on the host.
  The release workflow copies `deploy/` to S3 for each commit, and the host keeps each bundle under
  `/opt/codeatlas/releases/<sha>/` with `current` pointing at the running one, so a host always runs
  the scripts of the commit it serves (research R7).
- Application changes stay inside existing packages, except `codeatlas.ops` for operator commands,
  whose pure parts (`backup.py`) take plain data so unit tests cover them without a database.
- The only external boundaries are still `GitHubGateway` and the model adapters; the application
  itself never calls AWS.

## Complexity Tracking

| Violation | Why needed | Simpler alternative rejected because |
| --- | --- | --- |
| Scheduled writes to the project's own AWS account run without a confirmation each time: the nightly backup upload and its metric, the certificate metric, the worker's and the agent's metrics, and alert emails. The rule says a confirmation covers only the draft shown | FR-014 and FR-015 require daily backups and continuous metrics and alerts; nobody can confirm each one. The developer confirms them once, in the Terraform plan and the release that install the timers, roles, and alarms, and each write does only what that preview showed: fixed destinations, no overwrite or delete of backups, and no write outside the account | A confirmation per backup or per alert defeats the purpose of both. Leaving out scheduled backups or alerts violates FR-014 and FR-015 |

Every design-document mechanism left out is a simplification, recorded with its revisit condition
in research.md.
