# Data Model: Automatic Re-indexing and Access Revocation

Phase 1 output for [plan.md](plan.md). This document lists only what changes relative to
[001's data model](../001-repository-qa/data-model.md); everything else stays as it is. One
Alembic migration, `0002_push_reindexing`, makes all the changes below.

## Spec entities and where they live

| Spec entity | Storage |
| --- | --- |
| Change notification | New table `webhook_deliveries` |
| Installation | No table. Its status shows on each covering repository as `access_state` and `access_reason` (research, deviations table) |
| Repository (extended) | New columns on `repositories` |
| Indexing run (extended) | New `trigger` column and new payload field on `jobs` |
| Audit event (extended) | New `action` values on `audit_events` |

## repositories: new columns

| Field | Type | Rules |
| --- | --- | --- |
| access_state | text | `active`, `paused`, or `access_lost`; default `active` |
| access_reason | text | Null when `active`. Pause reasons: `sign_in_required`, `external_processing_not_accepted`. Loss reasons: `repository_not_visible`, `app_not_installed`, `installation_not_accessible`, `installation_cannot_read`, `app_uninstalled`, `app_suspended`, `repository_removed_from_installation` |
| access_lost_at | timestamptz | Set at the first detection of loss; cleared at restoration |
| access_checked_at | timestamptz | Time of the latest definitive access result, verified or lost (research R6) |
| latest_push_sha | text | The `after` SHA of the latest default-branch push received |
| latest_push_at | timestamptz | When that push was received |
| latest_push_job_id | uuid | FK jobs, on delete set null; the job that covers that push |

Constraints:

- `access_state IN ('active', 'paused', 'access_lost')`.
- `(access_state = 'active') = (access_reason IS NULL)`.
- `(access_state = 'access_lost') = (access_lost_at IS NOT NULL)`.
- Existing rows migrate to `active`. `access_checked_at` stays null, so the first scheduler pass
  checks them.

Access state transitions:

```text
active      --(definitive loss: run or notification)------> access_lost
paused      --(definitive loss: notification)--------------> access_lost
active      --(sign-in required | disclosure required)-----> paused
paused      --(owner signs in | owner accepts disclosure)--> active
access_lost --(a run's access check passes)----------------> active
access_lost --(lost for more than 7 days; maintenance)-----> tombstoned (deleted_at), then purged
```

- A run that finds sign-in required on a lost repository leaves it `access_lost`.
- Transient GitHub failures change nothing (research R4).

Display `state`, derived in this order of precedence:

1. `access_lost` when `access_state = 'access_lost'` (new);
2. then `indexing`, `ready`, `rejected`, and `failed`, exactly as in 001.

A pause does not change `state`. The API reports it separately as `automatic_updates`.

Reads:

- When `access_state = 'access_lost'`, every content read returns 403 `repository_access_lost`
  (research R7). This covers snapshots and coverage, trees, files, search, jobs and job events,
  and answers.
- `paused` does not restrict reads.

## jobs: changes

| Field | Change |
| --- | --- |
| trigger | New. Text, not null, default `user`; one of `user`, `push`, or `check` (research R3) |
| payload | Indexing payloads may add `pushed_commit_sha`, the push that the job covers, for SC-001 measurement and display |
| created_by | Null for automatic jobs (`push`, `check`) |

Constraint change:

- The partial unique index on `(workspace_id, dedupe_key)` now covers
  `status IN ('queued', 'retry_wait')` instead of all active statuses. A running job and one
  waiting job may therefore share a dedupe key (FR-004).
- The unique index allowing one running job per kind per workspace is unchanged (001 FR-027).

Trigger rules when requests merge:

- A waiting `check` job takes the trigger of a `push` or `user` request that merges into it.
- Other merges keep the existing trigger.

New permanent failure codes:

- `github_sign_in_required`: retryable after the owner signs in.
- `external_processing_not_accepted`.
- `github_app_misconfigured`: not permanent; retried like `github_unavailable`.

## snapshots

No new columns. A snapshot's trigger is read from its `job_id`. A version reused by a later run
keeps the trigger of the run that built it.

## webhook_deliveries (new)

Only verified deliveries are stored here. Rejected requests appear only as audit events (research
R2). The table is not tenant-owned, has no `workspace_id`, and is never returned by the API.

| Field | Type | Rules |
| --- | --- | --- |
| delivery_id | text | PK; the `X-GitHub-Delivery` GUID, which is unchanged when GitHub redelivers |
| event | text | `X-GitHub-Event`, for example `push` |
| action | text | Nullable; the payload `action` |
| github_installation_id | bigint | Nullable |
| github_repository_id | bigint | Nullable; set for `push` |
| outcome | text | `processed` or `ignored` |
| detail | jsonb | Safe fields only: `ref`, `after`, the number of matched repositories, and the reason for an `ignored` outcome. No commit messages, file names, or author data |
| received_at | timestamptz | |

Index: `received_at`, for retention.

## audit_events: new actions

| Action | Actor | Resource | Detail |
| --- | --- | --- | --- |
| `webhook_rejected` | null | `webhook` / null | `{"event": "<header>", "reason": "signature_missing" or "signature_mismatch"}` |
| `repository_access_lost` | null | `repository` | `{"reason": "...", "trigger": "push" or "check" or "user" or "notification"}` |
| `repository_access_restored` | null | `repository` | `{"trigger": "..."}` |
| `automatic_updates_paused` | null | `repository` | `{"reason": "sign_in_required" or "external_processing_not_accepted"}` |
| `automatic_updates_resumed` | the owner | `repository` | `{"via": "sign_in" or "acceptance"}` |
| `repository_disconnect` (existing) | null when automatic | `repository` | `{"reason": "access_lost"}` when the grace period ends |

## Retention summary (changes only)

| Data | Removed by the maintenance pass |
| --- | --- |
| Repositories lost for more than 7 days | Tombstoned, then purged in the same pass, together with everything under them |
| Ready snapshots that are neither active nor referenced by an unexpired run | When 5 or more newer ready versions of the same repository exist, or 14 days after creation, whichever comes first |
| `webhook_deliveries` | After 14 days |
