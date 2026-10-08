# Data Model: Pilot Deployment

Phase 1 output for [plan.md](plan.md). One migration, `0004_pilot_access.py` with revision ID
`0004` (following `0001` to `0003`), adds two tables and new audit actions. Every other table is
unchanged from 003. Release and backup records live outside the database.

## Spec entities and where they live

| Spec entity | Storage |
| --- | --- |
| Pilot user | New table `pilot_users` (research R13) |
| Pilot-wide usage allowance | New table `pilot_usage_counters` (research R14) |
| Audit event (extended) | New `action` values on `audit_events`; denied sign-ins use the existing outcome `denied` |
| Release | Not in the database: the GitHub deployment history for the `pilot` environment (commit, approver, times), the release summary in the workflow run (schema changes), and the Run Command output in the log group `/codeatlas/pilot/releases`, whose `release.sh` exit status gives the outcome: 0 released, 2 stopped before any change, 3 rolled back, 4 rollback failed (research R7) |
| Backup | Not in the database: `s3://<bucket>/backups/<UTC date>/codeatlas.dump` and its `manifest.json` (research R12) |

## pilot_users (new)

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

## pilot_usage_counters (new)

| Field | Type | Rules |
| --- | --- | --- |
| usage_date | date | Primary key; the UTC day |
| questions_count | integer | Default 0; at most `PILOT_DAILY_QUESTION_LIMIT` (default 30) |
| reviews_count | integer | Default 0; at most `PILOT_DAILY_REVIEW_LIMIT` (default 15) |

- Incremented by a guarded upsert that succeeds only while the count is below the limit, in the
  same transaction as the workspace counter (`usage_counters`). Either refusal rolls back both.
- `refund_review` decrements both counters for the review's usage date, never below zero.
- One row per day; rows are kept, as `usage_counters` rows are.

## audit_events: changes

| Field | Change |
| --- | --- |
| action | Adds `pilot_user_add`, `pilot_user_remove`, and `pilot_user_delete_data` |
| outcome | Unchanged; denied sign-ins use `denied` with action `sign_in` |

The pilot-user actions have no `actor_user_id` (the developer acts through the host, not a
session); their detail records the target's `github_user_id` and `github_login`. The repository
disconnections that `delete-data` makes record `repository_disconnect` as 001 does, with the user
as actor; the `pilot_user_delete_data` event records that the developer started them. The
migration's downgrade deletes rows with the new actions before restoring the old check.

## Backup manifest (`manifest.json`)

Built by `python -m codeatlas.ops backup-manifest` from the counts file that `backup.sh` writes in
the dump's own transaction, and read by `verify-restore`.

```json
{
  "created_at": "2026-10-08T03:30:12Z",
  "release": "<commit SHA running when the backup was taken>",
  "alembic_revision": "0004",
  "dump": { "key": "backups/2026-10-08/codeatlas.dump", "bytes": 734003200, "sha256": "<hex>" },
  "row_counts": { "users": 6, "workspaces": 6, "repositories": 21, "snapshots": 48, "...": 0 }
}
```

- The counts file (`counts.json`) holds `alembic_revision` and `row_counts` for every table in the
  `public` schema. `backup.sh` produces it in the same repeatable-read transaction whose exported
  snapshot `pg_dump` uses, so the counts describe exactly the dumped data (research R12).
- `verify-restore` passes when the restored database's Alembic revision equals the manifest's and
  every table's row count equals the manifest's; otherwise it prints each difference.

## Settings added

| Setting | Default | Notes |
| --- | --- | --- |
| `ACCESS_LIST_REQUIRED` | `0` | The access list always applies in production; this enables it elsewhere (tests) |
| `PILOT_USER_LIMIT` | `10` | Maximum rows in `pilot_users` |
| `PILOT_DAILY_QUESTION_LIMIT` | `30` | Pilot-wide; FR-005 |
| `PILOT_DAILY_REVIEW_LIMIT` | `15` | Pilot-wide; FR-005 |
| `RATE_LIMIT_PER_MINUTE` | `60` | Per client address on `/auth/*` and `/webhooks/*`; 0 disables |
| `EMIT_METRICS` | `0` | Writes EMF metric lines; render-config forces 0 outside the pilot |
| `METRICS_ENVIRONMENT` | `local` | The `Environment` dimension of the metrics; render-config sets it to the host's environment name |
| `CODEATLAS_RELEASE` | `development` | Commit SHA baked into the image at build time; reported by `/version` |

In hosted environments, `render-config.sh` derives `APP_ORIGIN` (`https://<hostname>`),
`DATABASE_URL`, and `METRICS_ENVIRONMENT`; they are not SSM parameters. Production validation
(research R5) also requires the existing GitHub App, webhook, token encryption, and model provider
settings, a readable private key file, an `https` `APP_ORIGIN`, and a `DATABASE_URL` other than the
development default.
