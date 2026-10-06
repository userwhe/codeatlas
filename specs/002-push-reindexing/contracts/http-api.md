# HTTP API Contract Changes: Automatic Re-indexing and Access Revocation

This document lists only changes to [001's HTTP API contract](../../001-repository-qa/contracts/http-api.md).
Conventions, pagination, idempotency, and the error shape are unchanged. The new inbound webhook
endpoint is in [github-webhooks.md](github-webhooks.md).

## New error

| Status | Code | When |
| --- | --- | --- |
| 403 | `repository_access_lost` | The repository belongs to the caller's workspace, but its access is lost |

The error's `details` contain:

```json
{ "repository_id": "uuid", "reason": "app_uninstalled", "lost_at": "2026-10-06T09:12:00Z", "purge_after": "2026-10-13T09:12:00Z" }
```

`retryable` is `false`.

The following endpoints return this error for a lost repository:

- `GET /v1/repositories/{id}/snapshots`;
- every `/v1/snapshots/{id}/*` endpoint;
- `POST /v1/search`;
- `POST /v1/analysis-runs`, `GET /v1/analysis-runs?repository_id=`, and
  `GET /v1/analysis-runs/{id}`;
- `GET /v1/jobs/{id}` and `GET /v1/jobs/{id}/events`.

Resources in another workspace still return 404 `not_found` (001 FR-004).

## Repository representation

`GET /v1/repositories` and `GET /v1/repositories/{id}` still return lost repositories. The
`Repository` object gains these fields:

```json
{
  "state": "access_lost",
  "access": {
    "state": "lost",
    "reason": "app_uninstalled",
    "checked_at": "2026-10-06T09:12:00Z",
    "lost_at": "2026-10-06T09:12:00Z",
    "purge_after": "2026-10-13T09:12:00Z"
  },
  "automatic_updates": { "state": "on", "reason": null },
  "latest_push": {
    "commit_sha": "9b1c...",
    "received_at": "2026-10-06T09:10:41Z",
    "job": { "id": "uuid", "status": "succeeded", "trigger": "push", "error": null }
  },
  "latest_indexing_job": { "id": "uuid", "status": "running", "trigger": "push", "error": null }
}
```

- **`state`** gains the value `access_lost`, which takes precedence over the others (data model).
- **`access`**:
  - `access.state` is `verified`, `lost`, or `unknown`. It is `unknown` when no check has finished
    yet.
  - `checked_at` is the latest definitive access result (FR-009).
  - `lost_at` and `purge_after` are null unless `access.state` is `lost`. `purge_after` is
    `lost_at` plus 7 days.
- **`automatic_updates`**:
  - `automatic_updates.state` is `on` or `paused`.
  - `reason` is `sign_in_required` or `external_processing_not_accepted` when paused, and null
    otherwise.
  - For a lost repository it reads `on`, because pushes still start access checks.
- **`latest_push`**:
  - It is null until a default-branch push is received.
  - `job` is null when the repository was paused at the time of the push.
- **`latest_indexing_job.trigger`**, and every job reference, is `user`, `push`, or `check`.

## `POST /v1/repositories/{repository_id}/index` (changed)

Request:

```json
{ "branch": "main", "accept_external_processing": true }
```

- **`accept_external_processing`**: optional, default `false`. It is required when the repository
  is paused with `external_processing_not_accepted`. Sending `true` records the acceptance,
  resumes automatic updates, and queues the run.
- **Lost repository**: re-indexing is allowed. The queued job checks access first, and restores the
  repository if access has returned (research R7).

New errors:

| Status | Code | Condition |
| --- | --- | --- |
| 422 | `external_processing_not_accepted` | The repository is paused for disclosure, and the request does not accept it |

## `GET /v1/repositories/{repository_id}/snapshots` (changed)

Each item gains a `trigger` field: `user`, `push`, or `check`, taken from the run that built the
version.

## `GET /v1/jobs/{job_id}` (changed)

The response gains `trigger`. For a `check` job that found nothing to index, the final event is
`succeeded` with the message "Already up to date".

Job error codes added: `github_sign_in_required`, `external_processing_not_accepted`, and
`github_app_misconfigured`.

## Sign-in (behavior change)

`GET /auth/github/callback` also resumes the owner's repositories that were paused with
`sign_in_required`, and queues a `check` run for each (research R8).
