# Data Model: Pull Request Review

Phase 1 output for [plan.md](plan.md). This document lists only the changes relative to the data
models of [001](../001-repository-qa/data-model.md) and [002](../002-push-reindexing/data-model.md);
everything else stays as it is. One Alembic migration, `0003_pull_request_review`, makes all the
changes below. No table is added.

## Spec entities and where they live

| Spec entity | Storage |
| --- | --- |
| Pull request | Not stored. It is read live from GitHub (research R1). What a review needs is copied onto its run at submission (`pull_request`, `pull_request_number`, and the pinned commits) |
| Pull request review | An `analysis_runs` row with `kind = 'pull_request_review'` |
| Risk, checklist item, test suggestion | Inside `analysis_runs.result` (shape below) |
| Evidence item (extended) | `evidence_items`, with a new `side` column and new source types |
| Usage allowance (extended) | New `reviews_count` column on `usage_counters` |
| Audit event (extended) | New `action` value on `audit_events` |

## analysis_runs: changes

| Field | Change |
| --- | --- |
| kind | Adds `pull_request_review` |
| snapshot_id, index_version | Now nullable. Required for `repository_qa`; null for reviews (research R3) |
| question | Now nullable. Required, 1 to 2,000 characters, for `repository_qa`; null for reviews |
| commit_sha | For reviews, the pinned head commit (equal to `head_sha`) |
| pull_request_number | New. Integer; required for reviews, null otherwise |
| base_sha | New. Text, 40 hex characters; the base branch tip at submission |
| head_sha | New. Text, 40 hex characters; the head commit at submission |
| merge_base_sha | New. Text, 40 hex characters; null until the job's `resolving_commits` stage resolves it (research R2) |
| pull_request | New. jsonb, copied at submission: `title`, `body` (at most 8,000 characters), `author`, `base_ref`, `head_ref`, `head_repository`, `is_fork`, `draft`, `html_url`, `additions`, `deletions`, `changed_files` |
| quality_state | Adds `reviewed` and `nothing_to_review` |
| result | For reviews, the validated review (shape below) |
| prompt_version | The review prompt's version, for example `review-v2` |

Constraints:

- `kind = 'repository_qa'` requires `snapshot_id`, `index_version`, and `question` to be non-null,
  and the pull request columns to be null. The existing question-length check applies when
  `question` is not null.
- `kind = 'pull_request_review'` requires `pull_request_number`, `base_sha`, `head_sha`, and
  `pull_request` to be non-null, `snapshot_id`, `index_version`, and `question` to be null, and
  `commit_sha = head_sha`.
- `quality_state` is null, or `answered` and `insufficient_evidence` for `repository_qa`, or
  `reviewed` and `nothing_to_review` for reviews.
- New index `(repository_id, pull_request_number, head_sha, created_at)` where
  `kind = 'pull_request_review'`. It serves reuse lookups (research R9) and the latest review per
  pull request in the list.
- Existing rows are all `repository_qa` and satisfy the new checks unchanged.

The pull request title and description are untrusted. They are stored to show the review and to
build the prompt, and are never logged.

Review state shown per pull request in the list (derived, not stored), from the newest run for
that pull request:

| Newest run | State shown |
| --- | --- |
| None | `none` |
| Job `queued` | `queued` |
| Job `running` or `retry_wait` | `running` |
| Job `succeeded`, `head_sha` equal to the pull request's current head | `current` |
| Job `succeeded`, `head_sha` different | `outdated` |
| Job `failed` or `canceled` | `failed` |

### Review result shape (`analysis_runs.result`)

```json
{
  "overall_risk": { "level": "high", "partial": false },
  "overview": "Removes the permission check from repository reads and adds a cache.",
  "summary": [
    { "area": "app/auth", "points": [
      { "change": "modified", "text": "check_access no longer verifies the role.", "evidence_ids": ["E1", "E2"], "origin": "model" }
    ] }
  ],
  "risks": [
    { "id": "R1", "title": "Role check removed", "severity": "high", "category": "security",
      "basis": "observed", "explanation": "...", "suggested_check": "...",
      "evidence_ids": ["E2"], "origin": "model", "path": null }
  ],
  "checklist": [ { "text": "Confirm that viewers cannot write.", "paths": ["app/auth/access.py"], "risk_ids": ["R1"] } ],
  "tests": {
    "changed": [ { "path": "tests/test_format.py", "change": "added" } ],
    "candidates": [ { "path": "tests/test_access.py", "reason": "refers to `check_access`", "evidence_ids": ["E9"] } ],
    "new_cases": [ { "behavior": "A viewer is refused on write.", "location_hint": "tests/test_access.py", "evidence_ids": ["E1"] } ]
  },
  "coverage": {
    "files": [ { "path": "app/auth/access.py", "previous_path": null, "change": "modified",
                 "additions": 3, "deletions": 9, "reviewed": true, "reason": null } ],
    "changed_files": 1, "reviewed_files": 1, "changed_lines_reviewed": 12,
    "context_items": 3
  },
  "omitted_items": 0
}
```

Rules:

- `overall_risk.level` is `high`, `medium`, `low`, or `none`: the highest severity among `risks`.
  `partial` is true when any coverage entry has the reason `review_limit` (research R8).
- `risks` are ordered by severity and numbered `R1` to `Rn`. `origin` is `model` or `rule`. A
  `rule` risk names a credential file in `path` and has no `evidence_ids` (FR-019).
- `summary[].area` is derived by the server from the first cited path (research R8). A summary
  point's `origin` is `model`, or `rule` for a file renamed without changes. A `rule` point has no
  `evidence_ids`, and its area comes from the new path.
- Every `evidence_ids` entry names a stored evidence item of the run. Model summary points and
  new test cases cite at least one `change` item.
- `omitted_items` counts model items dropped by validation, including checklist items left with no
  valid path or risk (research R7).
- The API returns each `evidence_ids` list as `citations`, and `coverage` and `omitted_items` at
  the levels shown in [contracts/http-api.md](contracts/http-api.md).
- `coverage.files[].change` is `added`, `modified`, `renamed`, or `removed`. `reason` is null when
  reviewed, and otherwise one of 001's coverage reasons, or `review_limit`. A changed excluded
  directory is one entry with `"entry_type": "directory"` and a `count`.
- For `nothing_to_review`, `overview`, `summary`, `checklist`, and `tests.new_cases` are empty, and
  `risks` holds only rule risks.

## evidence_items: changes

| Field | Change |
| --- | --- |
| source_type | Adds `change` (lines of the diff), `reference` (related code), and `test` (candidate test excerpt) |
| side | New. Text, nullable: `before` (the merge base) or `after` (the head). Required for the three new source types, null for 001's types |
| commit_sha | For reviews, `merge_base_sha` when `side = 'before'`, and `head_sha` when `side = 'after'` |
| label | `E1` to `En`, at most 200 per review |

Constraints:

- `side IN ('before', 'after')` or null.
- `(source_type IN ('change', 'reference', 'test')) = (side IS NOT NULL)`.
- `reference` and `test` items have `side = 'after'`.
- Line ranges are checked against the side's file before the row is written (FR-016).

Evidence items and the run's result are written in the same fenced transaction, as in 001.

## usage_counters: changes

| Field | Change |
| --- | --- |
| reviews_count | New. Integer, not null, default 0. Incremented in the transaction that creates a review, only while below `daily_review_limit`, default 10 (FR-027) |

- A review counts when it is accepted, whatever its outcome, with one exception. A run that ends
  `nothing_to_review` decrements the counter of its creation day in its publishing transaction,
  never below zero (FR-020).
- A reused review (`reused: true`) does not count.
- The allowance resets at 00:00 UTC, as for questions.

## jobs: changes

| Field | Change |
| --- | --- |
| kind | Adds `review_pull_request` |
| analysis_run_id | Set for `review_pull_request` |
| dedupe_key | `run:{run_id}`, as for questions |
| deadline_at | Set at first start to 5 minutes (`review_deadline`) |
| trigger | Always `user` |

The unique index allowing one running job per kind per workspace is unchanged. It now also limits
each workspace to one running review (FR-026).

Job event stages for reviews: `checking_access`, `resolving_commits`, `fetching_source`,
`comparing`, `gathering_context`, `generating_review`, `validating_citations`, and `publishing`.
Messages and data hold counts only.

New failure codes:

| Code | Permanent | Retryable by the user | Meaning |
| --- | --- | --- | --- |
| `commit_unavailable` | Yes | No | GitHub no longer serves a pinned commit (FR-021) |
| `no_common_history` | Yes | No | The base and head share no history (FR-021) |
| `review_validation_failed` | Yes | Yes | The model output did not parse after the repair call |

Reused codes: `access_denied` and the 002 access-loss outcomes, `github_sign_in_required`,
`github_unavailable`, `limit_exceeded`, `provider_unavailable`, `model_refused`, and the timeout
failure.

## audit_events: new action

| Action | Actor | Resource | Detail |
| --- | --- | --- | --- |
| `pull_request_review_submit` | the user | `analysis_run` | `{"pull_request_number": 12, "mode": "reuse" or "new", "reused": true or false}` |

Denied reads of reviews use the existing `access_denied` action. Detail never holds titles,
descriptions, or code.

## Retention summary (changes only)

| Data | Removal |
| --- | --- |
| Reviews and their evidence | After `expires_at` (30 days), like answers (FR-033). The maintenance pass already deletes expired runs, and evidence cascades |
| Reviews of a disconnected or purged repository | Inaccessible at once and purged with the repository, as in 001 and 002 |

Reviews reference no snapshot, so they do not keep snapshots alive and do not count toward 002's
cap on superseded versions.
