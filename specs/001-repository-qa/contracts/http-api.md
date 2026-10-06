# HTTP API Contract: Repository Q&A with Citations

This is the contract surface for this increment. FastAPI generates the precise OpenAPI document
from the Pydantic models, and the frontend uses types generated from that document. Data shapes
refer to [data-model.md](../data-model.md).

## Conventions

- **Origin**: The web app and the API share one origin. `/auth/*` and `/v1/*` are served by the
  API; every other path is served by the web app.
- **Authentication**: A `codeatlas_session` cookie. If it is missing or invalid, the response is
  401 `unauthenticated`.
- **CSRF**: `POST` and `DELETE` requests must send an `Origin` header equal to the app origin.
  Otherwise the response is 403 `origin_mismatch`.
- **Idempotency**: Endpoints marked *idempotent* accept an `Idempotency-Key` header of 1 to 128
  characters.
  - Replaying the same key with the same body returns the stored response.
  - Replaying the same key with a different body returns 409 `idempotency_conflict`.
  - Keys expire after 24 hours.
- **Pagination**: List endpoints take `?cursor=<opaque>&limit=<1..100, default 25>` and return
  `{ "items": [...], "next_cursor": string | null }`.
- **Visibility**: A resource in another workspace, or a disconnected repository, returns 404
  `not_found`, exactly as if it did not exist.
- **Errors**: Every error uses this shape:

```json
{
  "error": {
    "code": "daily_limit_reached",
    "message": "This workspace has used 20 of 20 questions today.",
    "retryable": false,
    "request_id": "req_01J...",
    "details": { "resets_at": "2026-10-04T00:00:00Z" }
  }
}
```

| Status | Used for |
| --- | --- |
| 401 | No valid session (`unauthenticated`), or GitHub sign-in needed again (`github_sign_in_required`) |
| 403 | Origin check failed |
| 404 | Missing or inaccessible resource |
| 409 | Incompatible state, idempotency conflict, repository already connected, repository limit reached |
| 422 | Invalid input, including a private repository without the processing acceptance |
| 429 | Daily question allowance exhausted |
| 502 | GitHub unavailable during a synchronous call (`retryable: true`) |

## Authentication

### `GET /auth/github/login`

Redirects (302) to GitHub's App user-authorization page. It sets a short-lived, signed
`oauth_state` cookie.

### `GET /auth/github/callback?code=&state=`

1. Validates `state` against the cookie.
2. Exchanges `code` for user tokens.
3. Creates or updates the user. On first sign-in, it also creates the personal workspace and the
   owner membership.
4. Creates a session and sets `codeatlas_session`.
5. Redirects (302) to `/repositories`.

On failure, it redirects to `/?error=sign_in_failed` and records an audit event.

### `POST /auth/logout`

Revokes the session and clears the cookie. Response: 204.

## Account

### `GET /v1/me`

```json
{
  "user": { "id": "uuid", "github_login": "octocat", "name": "The Octocat", "avatar_url": "https://..." },
  "workspace": { "id": "uuid", "name": "octocat" },
  "github_app_install_url": "https://github.com/apps/<app-slug>/installations/new"
}
```

### `GET /v1/usage`

```json
{ "usage_date": "2026-10-03", "questions_used": 3, "questions_limit": 20, "resets_at": "2026-10-04T00:00:00Z" }
```

## Repositories

### `GET /v1/github/repositories` (paginated)

Lists the repositories the signed-in user can access through installations of the CodeAtlas App.
These are the candidates for connection.

```json
{
  "items": [
    {
      "github_repository_id": 123456,
      "full_name": "octo-org/service",
      "private": true,
      "default_branch": "main",
      "connected_repository_id": null
    }
  ],
  "next_cursor": null
}
```

### `POST /v1/repositories` (idempotent)

Request:

```json
{ "github_repository_id": 123456, "branch": "main", "accept_external_processing": true }
```

- `branch` is optional and defaults to the default branch.
- `accept_external_processing` must be `true` for a private repository.
- The endpoint makes at most two GitHub calls: the access check and the installation lookup.
  Branch resolution happens in the indexing job. An unknown branch fails that job with
  `branch_not_found`, and a repository with no branches fails it with `repository_empty`.

Response 202:

```json
{ "repository": { "...": "Repository" }, "job": { "id": "uuid", "status": "queued" } }
```

Errors:

| Status | Code | Condition |
| --- | --- | --- |
| 401 | `github_sign_in_required` | The stored GitHub authorization is missing, expired, or unreadable. Unreadable credentials are deleted and the user's sessions end |
| 404 | `not_found` | The repository is not accessible to the user through the App |
| 409 | `already_connected` | The repository is already connected in this workspace |
| 409 | `repository_limit_reached` | The workspace already has 10 connected repositories |
| 422 | `external_processing_not_accepted` | A private repository without the acceptance |

### `GET /v1/repositories` (paginated)

Returns the workspace's connected repositories as `Repository` items.

### `GET /v1/repositories/{repository_id}`

Repository:

```json
{
  "id": "uuid",
  "full_name": "octo-org/service",
  "private": true,
  "default_branch": "main",
  "state": "indexing",
  "active_snapshot": { "id": "uuid", "commit_sha": "4f2c...", "branch": "main", "ready_at": "..." },
  "latest_indexing_job": {
    "id": "uuid",
    "status": "running",
    "error": null
  },
  "created_at": "..."
}
```

- `state` is one of `indexing`, `ready`, `rejected`, or `failed`. It is derived; see the data
  model.
- `active_snapshot` may be `null`.
- `latest_indexing_job.error` is either `null` or an object with `code`, `message`, and
  `retryable`.

### `DELETE /v1/repositories/{repository_id}`

Disconnects the repository. Response: 204. It sets the tombstone immediately, cancels queued
jobs, and fences running ones. Every later read returns 404 (FR-034).

### `POST /v1/repositories/{repository_id}/index` (idempotent)

Request:

```json
{ "branch": "main" }
```

`branch` is optional and defaults to the default branch. The endpoint makes no GitHub call and
returns 202 with `{ "job": { "id": "uuid", "status": "queued" } }`. The job re-checks access and
resolves the branch to a commit.

- If a ready snapshot already exists for the resolved commit and the current index version, the
  job reuses it and succeeds.
- If an indexing job for the same branch is already active, that job is returned.

### `GET /v1/repositories/{repository_id}/snapshots` (paginated)

Items:

```json
{ "id": "uuid", "commit_sha": "4f2c...", "branch": "main", "status": "ready", "is_active": true,
  "index_version": "idx-3f9a", "coverage": { "...": "Coverage counts" }, "created_at": "...", "ready_at": "..." }
```

## Snapshots: browse and coverage

Every endpoint in this section returns 409 `snapshot_not_ready` unless the snapshot's status is
`ready`.

### `GET /v1/snapshots/{snapshot_id}`

Returns the snapshot and its coverage counts. When embeddings were unavailable during indexing,
the response includes `"embeddings_available": false`.

### `GET /v1/snapshots/{snapshot_id}/coverage` (paginated)

Items: `{ "path": "node_modules", "entry_type": "directory", "reason": "excluded_directory", "detail": null }`.

### `GET /v1/snapshots/{snapshot_id}/tree?path=<dir>`

Lists the immediate children of a directory. `path` is empty for the root.

```json
{ "path": "src", "entries": [ { "name": "api", "type": "directory" }, { "name": "app.py", "type": "file", "language": "python", "line_count": 120 } ] }
```

### `GET /v1/snapshots/{snapshot_id}/file?path=<file>&start_line=<n>&end_line=<m>`

Returns at most 1,000 lines per request. Defaults are lines 1 to 1,000.

```json
{ "path": "src/app.py", "commit_sha": "4f2c...", "language": "python", "line_count": 120,
  "start_line": 1, "end_line": 120, "lines": ["import os", "..."] }
```

## Search

### `POST /v1/search`

Request:

```json
{ "snapshot_id": "uuid", "query": "permission", "mode": "text", "limit": 20 }
```

- `mode` is one of `text`, `path`, `symbol`, or `docs`.
- `query` is 1 to 200 characters.
- `limit` is 1 to 50, default 20.

Response:

```json
{
  "mode": "symbol",
  "degraded": false,
  "results": [
    { "path": "src/auth/access.py", "start_line": 14, "end_line": 41, "snippet": "def check_repository_access(...",
      "symbol": { "name": "check_repository_access", "kind": "function" }, "exact": true }
  ]
}
```

- In `symbol` and `path` modes, exact matches come first and are marked `exact: true`.
- `degraded: true` appears only in `docs` mode, when meaning-based search was unavailable and
  keyword results were returned (FR-017).

## Questions

A question and its answer are stored as an analysis run of kind `repository_qa`.

### `POST /v1/analysis-runs` (idempotent)

Request:

```json
{ "repository_id": "uuid", "kind": "repository_qa", "target": { "snapshot_id": "uuid" }, "question": "Where are repository permissions checked?" }
```

- `target.snapshot_id` is optional. When omitted, the repository's active snapshot is resolved
  once and pinned.
- `question` is 1 to 2,000 characters after trimming.

Response 202:

```json
{ "run_id": "uuid", "job_id": "uuid", "status": "queued", "snapshot_id": "uuid",
  "result_url": "/v1/analysis-runs/uuid", "events_url": "/v1/jobs/uuid/events" }
```

Errors:

| Status | Code | Condition |
| --- | --- | --- |
| 409 | `snapshot_not_ready` | The repository has no ready snapshot |
| 429 | `daily_limit_reached` | The daily allowance is used up; `details.resets_at` is included |
| 422 | `invalid_question` | The question is empty or too long |

### `GET /v1/analysis-runs?repository_id=<id>` (paginated)

Returns the answer history, newest first. Items contain `id`, `question`, `status`,
`quality_state`, `commit_sha`, and `created_at`.

### `GET /v1/analysis-runs/{run_id}`

```json
{
  "id": "uuid",
  "kind": "repository_qa",
  "status": "succeeded",
  "quality_state": "answered",
  "repository_id": "uuid",
  "snapshot_id": "uuid",
  "commit_sha": "4f2c...",
  "question": "Where are repository permissions checked?",
  "answer": {
    "summary": "Repository access is checked in ...",
    "claims": [
      { "text": "check_repository_access verifies installation access before indexing.", "kind": "fact", "citations": ["E1"] },
      { "text": "The same check likely guards search.", "kind": "inference", "citations": ["E2"] }
    ],
    "gaps": []
  },
  "citations": [
    { "label": "E1", "path": "src/auth/access.py", "commit_sha": "4f2c...", "start_line": 14, "end_line": 41,
      "excerpt": "def check_repository_access(...", "view_url": "/snapshots/uuid/browse?path=src/auth/access.py&lines=14-41" }
  ],
  "error": null,
  "created_at": "...",
  "completed_at": "..."
}
```

- `status` is the execution state, taken from the job: `queued`, `running`, `retry_wait`,
  `succeeded`, `failed`, or `canceled`.
- `quality_state` is `answered`, `insufficient_evidence`, or `null`.
- When `quality_state` is `insufficient_evidence`, `claims` may be empty and `gaps` lists what is
  missing.
- `error` is either `null` or an object with `code`, `message`, and `retryable`. For example, a
  provider outage gives `{ "code": "provider_unavailable", "retryable": true }` (FR-024).
- The server builds each `citations[].view_url` from stored evidence items, never from model
  output (FR-020).

## Jobs

### `GET /v1/jobs/{job_id}`

```json
{ "id": "uuid", "kind": "index_repository", "status": "queued", "attempt": 0,
  "queued_behind": 1, "error": null, "created_at": "...", "started_at": null, "finished_at": null }
```

`queued_behind` is the number of jobs of the same kind ahead of this one in the workspace queue.
It is present only when `status` is `queued` (FR-027).

### `GET /v1/jobs/{job_id}/events?after=<seq>`

```json
{
  "job_status": "running",
  "items": [
    { "seq": 4, "event_type": "stage_started", "stage": "parsing", "message": "Parsing 812 files", "data": { "files": 812 }, "created_at": "..." }
  ],
  "next_after": 4
}
```

Clients poll every 1.5 seconds while `job_status` is not terminal. Passing `after` returns only
the events after that sequence number, so a reconnecting client receives everything it missed
(FR-026).
