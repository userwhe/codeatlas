# Data Model: Pilot Deployment

Phase 1 output for [plan.md](plan.md). Two migrations: `0004_pilot_access.py` (revision `0004`,
already applied in development) adds the access list, the pilot-wide counters, and new audit
actions; `0005_operations_status.py` (revision `0005`) adds what the alert checks read: worker
heartbeats, completed backups, and whether a failed job's failure was permanent. Every other table
is unchanged from 003.

## Spec entities and where they live

| Spec entity | Storage |
| --- | --- |
| Pilot user | Table `pilot_users` (research R13) |
| Pilot-wide usage allowance | Table `pilot_usage_counters` (research R14) |
| Audit event (extended) | New `action` values on `audit_events`; denied sign-ins use the existing outcome `denied` |
| Release | Not in the database: the hosting platform's deployment history of each service (commit, status, start and end times; a failed build, pre-deploy command, or readiness check marks the deployment failed, and a rollback creates a new deployment of the earlier image), and the CI report on the pull request that says whether it changes the schema (research R7) |
| Backup | Table `backups` (one row per completed backup) and two objects in the backup bucket, `backups/<UTC date>/<UTC time>/codeatlas.dump` and its `manifest.json` beside it (research R12) |

## pilot_users (0004)

| Field | Type | Rules |
| --- | --- | --- |
| github_user_id | bigint | Primary key. GitHub's numeric user ID, resolved from the login when the user is added |
| github_login | text | The login when added, for display and commands. Updated at each sign-in, so a rename stays visible |
| note | text, nullable | At most 200 characters; free text from the developer, such as who invited the user |
| added_at | timestamptz | Default `now()` |

- No foreign key to `users`: a pilot user may be added before they first sign in.
- `add` refuses when the table already has `PILOT_USER_LIMIT` rows (default 10).
- Removing a row and revoking the user's sessions happen in one transaction (research R13).

**Access decision** at sign-in, when the access list applies (`CODEATLAS_ENV=production` or
`ACCESS_LIST_REQUIRED=1`):

| GitHub user ID in `pilot_users` | Result |
| --- | --- |
| Yes | Sign-in continues as in 001: user, workspace, tokens, session |
| No | Nothing is written except one `audit_events` row (`sign_in`, `denied`, detail `{"reason": "not_invited", "github_login": ...}`); redirect to `/?error=not_invited` |

## pilot_usage_counters (0004)

| Field | Type | Rules |
| --- | --- | --- |
| usage_date | date | Primary key; the UTC day |
| questions_count | integer | Default 0; at most `PILOT_DAILY_QUESTION_LIMIT` (default 30) |
| reviews_count | integer | Default 0; at most `PILOT_DAILY_REVIEW_LIMIT` (default 15) |

- Incremented by a guarded upsert that succeeds only while the count is below the limit, in the
  same transaction as the workspace counter (`usage_counters`). Either refusal rolls back both.
- `refund_review` decrements both counters for the review's usage date, never below zero.
- One row per day; rows are kept, as `usage_counters` rows are.

## audit_events: changes (0004)

| Field | Change |
| --- | --- |
| action | Adds `pilot_user_add`, `pilot_user_remove`, and `pilot_user_delete_data` |
| outcome | Unchanged; denied sign-ins use `denied` with action `sign_in` |

The pilot-user actions have no `actor_user_id` (the developer acts through an operator command,
not a session); their detail records the target's `github_user_id` and `github_login`. The
repository disconnections that `delete-data` makes record `repository_disconnect` as 001 does,
with the user as actor; the `pilot_user_delete_data` event records that the developer started
them. The migration's downgrade deletes rows with the new actions before restoring the old check.

## worker_heartbeats (0005)

| Field | Type | Rules |
| --- | --- | --- |
| worker_id | text | Primary key: the replica's host name and the process ID, `<hostname>:<pid>` |
| started_at | timestamptz | When the worker process started |
| beat_at | timestamptz | The reporter's last report; updated every 60 seconds |

- The worker's reporter thread upserts its own row every `WORKER_REPORT_SECONDS` (60) and, in the
  same transaction, deletes rows whose `beat_at` is more than 24 hours old, so replaced replicas do
  not accumulate.
- During a release two workers may report at once (the old one drains while the new one starts);
  the check reads only the newest `beat_at`.

## backups (0005)

| Field | Type | Rules |
| --- | --- | --- |
| id | uuid | Primary key |
| object_key | text | Unique; the dump's key, `backups/<UTC date>/<UTC time>/codeatlas.dump`, with the time as `HHMMSS`, so every run has its own keys |
| completed_at | timestamptz | When the dump and its manifest were both uploaded |
| bytes | bigint | The dump's size |
| sha256 | text | 64 lowercase hexadecimal characters |
| alembic_revision | text | The schema revision in the dump |
| release | text | The commit running when the backup was taken |

- `ops backup` inserts the row only after both objects are uploaded, so a row always means a
  complete backup. The row is written after the dump, so a backup's own row is not inside it;
  after a restore, the newest row is the one before the restored backup until the next nightly
  run (research R12).
- Each run of `ops backup` first prunes the backups dated before the last `BACKUP_RETENTION_DAYS`
  (7) UTC days, today included (dates from `today - 6` are kept), whether or not the rest of the run
  succeeds, and deletes their rows when the database answers, so the table lists the backups that
  still exist.

## jobs: changes (0005)

| Field | Change |
| --- | --- |
| error_permanent | New, `boolean`, nullable. Set with `error_code` when a job fails: whether its failure was permanent (not retried). Null for jobs that have not failed and for failures recorded before the migration |

The failing-jobs check counts a failure when it was not permanent (its retries were used up), or
when its code is `timeout` or `internal_error`, the same rule the earlier `JobsFailed` metric used
(`COUNTED_PERMANENT_CODES`). Failures caused by the input or by lost access are permanent with
other codes and do not count.

## Alert conditions (computed, not stored)

Each condition is read by its own status endpoint ([contracts/http-api.md](contracts/http-api.md))
and checked from outside the platform every 3 minutes (research R10).

| Condition | Failing when | Threshold setting (default) |
| --- | --- | --- |
| `worker` | No `worker_heartbeats` row has `beat_at` within the threshold | `WORKER_SILENT_SECONDS` (300) |
| `jobs-waiting` | The oldest job that `claim_next` could claim now (`runnable_waiting`: waiting, due, and no running job of its workspace and kind) became due longer ago than the threshold | `JOBS_WAITING_ALERT_SECONDS` (1800) |
| `jobs-failing` | A counted failure (above) finished within the last hour, and the counted failures that finished in the 60 minutes up to and including it number at least the threshold. The condition therefore holds for an hour after the third failure of a run. `value` is the largest such number, 0 when none | `JOBS_FAILING_ALERT_COUNT` (3) |
| `backup` | The newest `backups.completed_at` is older than the threshold, or the table is empty | `BACKUP_MISSING_HOURS` (26) |

A new environment has no backup until its first nightly run, so `backup` fails there until then;
the operations guide says to create the backup monitor only after the first backup completes.

## Backup manifest (`manifest.json`)

Built by `ops backup` with the existing pure functions in `codeatlas.ops.backup`, and read by
`ops verify-restore`.

```json
{
  "created_at": "2026-10-13T03:30:12Z",
  "release": "<commit SHA running when the backup was taken>",
  "alembic_revision": "0005",
  "dump": { "key": "backups/2026-10-13/033012/codeatlas.dump", "bytes": 734003200, "sha256": "<hex>" },
  "row_counts": { "users": 6, "workspaces": 6, "repositories": 21, "snapshots": 48, "...": 0 }
}
```

- `ops backup` counts the rows of every table in the `public` schema, and reads the Alembic
  revision, in the same repeatable-read transaction whose exported snapshot `pg_dump` uses, so the
  counts describe exactly the dumped data (research R12).
- `verify-restore` passes when the restored database's Alembic revision equals the manifest's and
  every table's row count equals the manifest's; otherwise it prints each difference. `ops restore`
  runs it on the restored schema before migrating it to the release's revision.

## Settings

| Setting | Default | Notes |
| --- | --- | --- |
| `ACCESS_LIST_REQUIRED` | `0` | The access list always applies in production; this enables it elsewhere (tests) |
| `PILOT_USER_LIMIT` | `10` | Maximum rows in `pilot_users` |
| `PILOT_DAILY_QUESTION_LIMIT` | `30` | Pilot-wide; FR-005 |
| `PILOT_DAILY_REVIEW_LIMIT` | `15` | Pilot-wide; FR-005 |
| `RATE_LIMIT_PER_MINUTE` | `60` | Per client address on `/auth/*`, `/webhooks/*`, and `/status/*`; 0 disables |
| `CODEATLAS_RELEASE` | `development` | The commit reported by `/version`. `RAILWAY_GIT_COMMIT_SHA`, which the platform sets for deployments from GitHub, takes precedence, because the image always defines `CODEATLAS_RELEASE` (changed; research R6) |
| `RAILWAY_GIT_COMMIT_SHA` | empty | New as a setting, `platform_commit`: the platform's commit alone. It replaces the release when set, and the release gate waits for it (research R6, R7) |
| `API_ORIGIN` | `http://localhost:8000` | New for the backend: the API's private address, which `ops wait-for-api` polls (research R7); the web app already reads it |
| `GITHUB_APP_PRIVATE_KEY` | empty | New: the App's private key itself, in PEM. Production needs this or a readable `GITHUB_APP_PRIVATE_KEY_PATH`; the content wins when both are set |
| `WORKER_REPORT_SECONDS` | `60` | New: how often the worker reports its heartbeat and queue state |
| `WORKER_SILENT_SECONDS` | `300` | New: `worker` condition |
| `JOBS_WAITING_ALERT_SECONDS` | `1800` | New: `jobs-waiting` condition |
| `JOBS_FAILING_ALERT_COUNT` | `3` | New: `jobs-failing` condition, per hour |
| `BACKUP_MISSING_HOURS` | `26` | New: `backup` condition |
| `BACKUP_ENDPOINT_URL`, `BACKUP_REGION`, `BACKUP_BUCKET`, `BACKUP_ACCESS_KEY_ID`, `BACKUP_SECRET_ACCESS_KEY` | empty | New: the S3-compatible bucket, set only on the backup service and wherever `ops restore` runs; `ops backup` and `ops restore` name each missing one |
| `BACKUP_RETENTION_DAYS` | `7` | New: FR-015 |

Removed: `EMIT_METRICS` and `METRICS_ENVIRONMENT`, which only fed CloudWatch's embedded metric
format.

On the platform, `APP_ORIGIN` and `DATABASE_URL` are reference variables built from the other
services' platform-provided domains and the database password, so a copied environment gets its
own values (research R5). Production validation also requires the existing GitHub App, webhook,
token encryption, and model provider settings, an `https` `APP_ORIGIN`, and a `DATABASE_URL` other
than the development default.
