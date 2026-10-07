# HTTP API Contract Changes: Pull Request Review

This document lists only changes to the HTTP API contracts of
[001](../../001-repository-qa/contracts/http-api.md) and
[002](../../002-push-reindexing/contracts/http-api.md). Conventions, pagination, idempotency, the
error shape, and the 403 `repository_access_lost` rule are unchanged. Data shapes refer to
[data-model.md](../data-model.md).

## New errors

| Status | Code | When |
| --- | --- | --- |
| 404 | `pull_request_not_found` | GitHub has no pull request with that number in the repository |
| 409 | `pull_request_not_open` | A review is requested for a closed or merged pull request (FR-024) |
| 409 | `repository_rejected` | The repository was rejected for a size limit (FR-003) |
| 409 | `pull_requests_permission_missing` | The installation has not approved `Pull requests: Read-only`. `details.settings_url` links to the installation's settings page on GitHub |
| 409 | `github_access_denied` | GitHub refused a read for another reason. Access loss is decided by the next access check, not by this call |
| 409 | `review_not_finished` | Markdown export of a review that has not succeeded |

Existing errors also used: 401 `github_sign_in_required` when the owner's GitHub authorization has
lapsed, 422 `external_processing_not_accepted` (002), 429 `daily_limit_reached`, 502
`github_unavailable`, and 403 `repository_access_lost`.

For `daily_limit_reached`, `details` gains `allowance`: `questions` or `reviews`.

## `GET /v1/repositories/{repository_id}/pull-requests`

Lists the repository's open pull requests, drafts included, live from GitHub, most recently
updated first (FR-001). Pagination follows GitHub's pages: `cursor` is an opaque page token, and
pages hold up to 30 items.

```json
{
  "items": [
    {
      "number": 12,
      "title": "Cache repository permissions",
      "author": "hubot",
      "draft": false,
      "base_ref": "main",
      "head_ref": "cache-permissions",
      "head_sha": "8d3f...",
      "is_fork": false,
      "updated_at": "2026-10-06T08:41:00Z",
      "html_url": "https://github.com/octo-org/sample-app/pull/12",
      "review": { "run_id": "uuid", "state": "current", "head_sha": "8d3f...", "created_at": "..." }
    }
  ],
  "next_cursor": null
}
```

- `review` is null when the pull request has never been reviewed. Otherwise `state` is `queued`,
  `running`, `current`, `outdated`, or `failed`, derived as in the data model.
- `title` and `author` come from GitHub and are untrusted; the client renders them as text.

Errors: 403 `repository_access_lost`, 409 `repository_rejected`, 409
`pull_requests_permission_missing`, 409 `github_access_denied`, 401 `github_sign_in_required`,
and 502 `github_unavailable`.

## `POST /v1/analysis-runs` (idempotent; new kind)

Request for a review:

```json
{ "repository_id": "uuid", "kind": "pull_request_review", "target": { "pull_request_number": 12 }, "mode": "reuse" }
```

- **`mode`**: `reuse` (default) or `new`. With `reuse`, an existing review of the same pull request
  and head commit that is waiting, running, or succeeded and unexpired is returned instead of a new
  one (FR-023).
- **`question`**: must be absent for this kind. `repository_qa` requests are unchanged.

Response 202 (new review) or 200 (reused):

```json
{ "run_id": "uuid", "job_id": "uuid", "status": "queued", "reused": false,
  "pull_request_number": 12, "head_sha": "8d3f...",
  "result_url": "/v1/analysis-runs/uuid", "events_url": "/v1/jobs/uuid/events" }
```

- For `repository_qa`, the response is unchanged, except that it also carries `reused: false`.

Errors for this kind:

| Status | Code | Condition |
| --- | --- | --- |
| 403 | `repository_access_lost` | The repository's access is lost |
| 404 | `pull_request_not_found` | No such pull request |
| 409 | `pull_request_not_open` | The pull request is closed or merged |
| 409 | `repository_rejected` | The repository was rejected for a size limit |
| 422 | `external_processing_not_accepted` | The repository is paused for the disclosure |
| 422 | `invalid_request` | A `question` was sent, or the number is not a positive integer |
| 429 | `daily_limit_reached` | The review allowance is used up; `details.allowance` is `reviews` |
| 401 | `github_sign_in_required` | The owner must sign in to GitHub again |
| 502 | `github_unavailable` | GitHub could not be reached; retryable |

## `GET /v1/analysis-runs?repository_id=<id>` (paginated; changed)

- New optional filter: `kind` (`repository_qa` or `pull_request_review`). The question history
  sends `kind=repository_qa`, so reviews never appear in it.
- Items gain `kind`, plus `pull_request_number`, `pull_request_title`, and `overall_risk_level`
  (null until the review succeeds) for reviews. `question` is null for reviews.
- The repository page's "Past reviews" list sends `kind=pull_request_review`. It is how reviews of
  closed and merged pull requests stay reachable (FR-024), because the pull request list shows
  only open pull requests.

## `GET /v1/analysis-runs/{run_id}` (changed)

For `kind = "pull_request_review"` (`repository_qa` responses are unchanged):

```json
{
  "id": "uuid",
  "kind": "pull_request_review",
  "status": "succeeded",
  "quality_state": "reviewed",
  "repository_id": "uuid",
  "repository_full_name": "octo-org/sample-app",
  "pull_request": {
    "number": 12, "title": "Cache repository permissions", "author": "hubot", "draft": false,
    "base_ref": "main", "head_ref": "cache-permissions", "head_repository": "octo-org/sample-app",
    "is_fork": false, "html_url": "https://github.com/octo-org/sample-app/pull/12"
  },
  "commits": { "base_sha": "4f2c...", "head_sha": "8d3f...", "merge_base_sha": "1a9e..." },
  "review": {
    "overall_risk": { "level": "high", "partial": false },
    "overview": "...",
    "summary": [ { "area": "app/auth", "points": [ { "change": "modified", "text": "...", "citations": ["E1", "E2"], "origin": "model" } ] } ],
    "risks": [ { "id": "R1", "title": "...", "severity": "high", "category": "security", "basis": "observed",
                 "explanation": "...", "suggested_check": "...", "citations": ["E2"], "origin": "model", "path": null } ],
    "checklist": [ { "text": "...", "paths": ["app/auth/access.py"], "risk_ids": ["R1"] } ],
    "tests": {
      "changed": [ { "path": "tests/test_format.py", "change": "added" } ],
      "candidates": [ { "path": "tests/test_access.py", "reason": "refers to `check_access`", "citations": ["E9"] } ],
      "new_cases": [ { "behavior": "...", "location_hint": "tests/test_access.py", "citations": ["E1"] } ]
    },
    "omitted_items": 0
  },
  "coverage": {
    "files": [ { "path": "app/auth/access.py", "previous_path": null, "change": "modified",
                 "additions": 3, "deletions": 9, "reviewed": true, "reason": null } ],
    "changed_files": 1, "reviewed_files": 1, "changed_lines_reviewed": 12, "context_items": 3
  },
  "citations": [
    { "label": "E2", "source_type": "change", "side": "before", "path": "app/auth/access.py",
      "commit_sha": "1a9e...", "start_line": 14, "end_line": 25, "excerpt": "def check_access(...",
      "github_url": "https://github.com/octo-org/sample-app/blob/1a9e.../app/auth/access.py#L14-L25" }
  ],
  "error": null,
  "job_id": "uuid",
  "created_at": "...",
  "completed_at": "..."
}
```

- `review` and `coverage` are null until the run succeeds. `merge_base_sha` is null until the
  first stage resolves it.
- `quality_state` is `reviewed`, `nothing_to_review`, or null.
- `citations` contains only the evidence items that the review cites. The server builds each
  `github_url` from stored evidence, never from model output (FR-016).
- The stored result's `evidence_ids` become `citations` here. Its `coverage` is returned at the top
  level, and its other fields under `review`.
- A `nothing_to_review` review has an empty `summary`, `checklist`, and `tests.new_cases`, and only
  rule risks.
- The response reads only the database. Freshness has its own endpoint.

## `GET /v1/analysis-runs/{run_id}/freshness` (new)

Reads the pull request from GitHub with the owner's user token (FR-022).

```json
{ "pull_request_state": "open", "draft": false, "current_head_sha": "9b1c...", "outdated": true, "checked_at": "2026-10-06T09:12:00Z" }
```

- `pull_request_state` is `open`, `closed`, or `merged`. `outdated` is true when
  `current_head_sha` differs from the review's `head_sha`.
- Only for reviews. A `repository_qa` run returns 404.

Errors: 403 `repository_access_lost`, 401 `github_sign_in_required`, 409 `github_access_denied`,
and 502 `github_unavailable`. The page then shows the freshness as unknown.

## `GET /v1/analysis-runs/{run_id}/markdown` (new)

Returns the succeeded review as Markdown (FR-015, research R11):

```json
{ "markdown": "## CodeAtlas review of #12 at 8d3f1c2\n\n**Overall risk: high**\n..." }
```

- Citations are links to GitHub at the cited commit.
- `@` mentions and `#` references from review text are wrapped in inline code, and raw HTML is
  escaped.
- Errors: 409 `review_not_finished`, and 404 for a `repository_qa` run.

## `GET /v1/usage` (changed)

```json
{ "usage_date": "2026-10-06", "questions_used": 3, "questions_limit": 20,
  "reviews_used": 1, "reviews_limit": 10, "resets_at": "2026-10-07T00:00:00Z" }
```

## `GET /v1/jobs/{job_id}` and `/events` (changed)

- `kind` gains `review_pull_request`.
- Review stages are listed in the data model. Job error codes gain `commit_unavailable`,
  `no_common_history`, and `review_validation_failed`.

## Web routes

- `/reviews/{run_id}`: the review page.
- `/repositories/{id}` gains the "Pull requests" section.
