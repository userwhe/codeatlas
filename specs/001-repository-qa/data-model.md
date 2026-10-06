# Data Model: Repository Q&A with Citations

Phase 1 output for [plan.md](plan.md). All tables live in PostgreSQL (see research R2). IDs are
UUIDs unless noted. Timestamps are `timestamptz` in UTC.

## Tenancy rules

- Every tenant-owned row carries `workspace_id`, directly or through a constrained parent.
- Child tables reference parents with composite foreign keys that include `workspace_id`.
  Examples: `snapshots (workspace_id, repository_id)` references
  `repositories (workspace_id, id)`, and `analysis_runs (workspace_id, snapshot_id)` references
  `snapshots (workspace_id, id)`. A row therefore cannot point into another workspace.
- Every query filters by the caller's `workspace_id` before it searches, ranks, paginates, or
  returns anything (FR-004, FR-018). A resource outside the caller's workspace returns 404.

## Identity

### users

| Field | Type | Rules |
| --- | --- | --- |
| id | uuid | PK |
| github_user_id | bigint | unique |
| github_login | text | updated at each sign-in |
| name, avatar_url | text | nullable |
| created_at, last_sign_in_at | timestamptz | |

### github_credentials

| Field | Type | Rules |
| --- | --- | --- |
| user_id | uuid | PK, FK users |
| access_token_enc | bytea | Fernet-encrypted user token |
| access_token_expires_at | timestamptz | |
| refresh_token_enc | bytea | Fernet-encrypted |
| refresh_token_expires_at | timestamptz | |
| updated_at | timestamptz | |

### sessions

| Field | Type | Rules |
| --- | --- | --- |
| token_hash | bytea | PK; SHA-256 of the cookie token |
| user_id | uuid | FK users |
| created_at, expires_at | timestamptz | `expires_at = created_at + 7 days` |
| revoked_at | timestamptz | set on sign-out (FR-005) |

A session is valid when `revoked_at IS NULL AND expires_at > now()`.

### workspaces and memberships

| Table | Fields | Rules |
| --- | --- | --- |
| workspaces | id, name, created_at | Created with the user at first sign-in (FR-002) |
| memberships | workspace_id, user_id, role, created_at | PK (workspace_id, user_id); `role = 'owner'` only in this increment |

## Repositories and indexed versions

### repositories

| Field | Type | Rules |
| --- | --- | --- |
| id | uuid | PK; unique (workspace_id, id) for composite FKs |
| workspace_id | uuid | FK workspaces |
| github_repository_id | bigint | GitHub's numeric ID, so renames keep identity |
| github_installation_id | bigint | Used to mint installation tokens; refreshed at each indexing, so a reinstalled App keeps working |
| full_name | text | `owner/name`; refreshed from `GET /repositories/{id}` at each indexing, so renames keep working |
| default_branch | text | Refreshed with `full_name` |
| is_private | boolean | |
| external_processing_accepted_at | timestamptz | Required before a private repository is indexed (FR-006) |
| active_snapshot_id | uuid | Nullable; the default ready snapshot |
| created_by | uuid | FK users |
| created_at | timestamptz | |
| deleted_at | timestamptz | Tombstone; set at disconnection (FR-034) |

Constraints and rules:

- Unique `(workspace_id, github_repository_id) WHERE deleted_at IS NULL`, so reconnecting after a
  disconnection creates a new row and indexes from scratch.
- At most 10 rows per workspace with `deleted_at IS NULL` (FR-009). This is checked in the
  connecting transaction under a row lock on the workspace.
- Display state is derived rather than stored:
  - `indexing`: an indexing job is queued or running;
  - `ready`: an active snapshot exists;
  - `rejected`: there is no active snapshot and the latest indexing failed with
    `limit_exceeded`;
  - `failed`: any other failure with no active snapshot.

### snapshots

| Field | Type | Rules |
| --- | --- | --- |
| id | uuid | PK; unique (workspace_id, id) |
| workspace_id, repository_id | uuid | Composite FK to repositories |
| commit_sha | text | 40 hex characters |
| branch | text | The branch that was resolved |
| index_version | text | Hash of parser versions, chunking settings, and embedding model and dimension |
| status | text | `building`, `ready`, `failed`, or `discarded` |
| job_id | uuid | The indexing job |
| job_attempt | integer | The attempt that built this row |
| coverage | jsonb | Counts: eligible_files, indexed_files, source_lines, symbols, code_chunks, doc_chunks, skipped_entries, plus `embeddings_available` |
| failure_code, failure_message | text | Set when `status = 'failed'` |
| created_at, ready_at | timestamptz | |

Constraint: unique `(repository_id, commit_sha, index_version) WHERE status = 'ready'`.
Re-indexing a commit that already has a ready snapshot with the same index version reuses that
snapshot.

State transitions:

```text
building --(fenced publish)--> ready
building --(attempt fails or loses lease)--> failed | discarded
ready --(maintenance: unreferenced, not active, older than 14 days)--> deleted
```

Publishing is one transaction with three steps:

1. Check the job's fencing token.
2. Set `status = 'ready'`.
3. Set `repositories.active_snapshot_id` to this snapshot, unless the repository is tombstoned.

A failed publish leaves the previous active snapshot untouched (FR-012).

### coverage_entries

| Field | Type | Rules |
| --- | --- | --- |
| id | bigint | PK |
| snapshot_id | uuid | FK snapshots, on delete cascade |
| path | text | Normalized repository-relative path |
| entry_type | text | `file` or `directory` |
| reason | text | `excluded_directory`, `credential_file`, `generated`, `binary`, `unsupported_encoding`, `too_large`, `link`, `unsafe_path`, `unsupported_syntax`, `embeddings_unavailable` |
| detail | text | Nullable, for example the size in bytes |

### files

| Field | Type | Rules |
| --- | --- | --- |
| id | uuid | PK |
| workspace_id, snapshot_id | uuid | Composite FK to snapshots |
| path | text | Normalized POSIX path; unique (snapshot_id, path) |
| language | text | `python`, `typescript`, `tsx`, `markdown`, or `text` |
| size_bytes, line_count | integer | `size_bytes <= 1 MiB` |
| content_sha256 | bytea | |
| content | text | UTF-8 contents |

Indexes: trigram GIN on `content` (text search) and on `path` (path search). Each snapshot holds
only the files present at its commit (FR-012), so deleted and renamed paths cannot carry over.

### symbols

| Field | Type | Rules |
| --- | --- | --- |
| id | uuid | PK |
| snapshot_id, file_id | uuid | FKs, on delete cascade |
| name | text | Declaration name |
| qualified_name | text | For example `Repo.connect` |
| kind | text | `class`, `function`, `method`, `interface`, `type_alias`, or `enum` |
| start_line, end_line | integer | 1-based, inclusive |

Indexes: B-tree on `(snapshot_id, name)` and trigram GIN on `name`.

### code_chunks

| Field | Type | Rules |
| --- | --- | --- |
| id | bigint | PK |
| snapshot_id, file_id | uuid | FKs, on delete cascade |
| start_line, end_line | integer | 60-line windows with a 10-line overlap |
| search_vector | tsvector | `simple` configuration over identifier-split text; GIN index |

### doc_chunks

| Field | Type | Rules |
| --- | --- | --- |
| id | bigint | PK |
| snapshot_id, file_id | uuid | FKs, on delete cascade |
| start_line, end_line | integer | |
| heading_path | text | For example `Setup > Configuration` |
| text | text | At most about 1,600 characters |
| search_vector | tsvector | `english` configuration; GIN index |
| embedding | vector(1024) | Nullable when embeddings were unavailable |

## Jobs and progress

### jobs

| Field | Type | Rules |
| --- | --- | --- |
| id | uuid | PK |
| workspace_id | uuid | FK workspaces |
| kind | text | `index_repository` or `answer_question` |
| repository_id | uuid | Composite FK to repositories |
| analysis_run_id | uuid | Set for `answer_question` |
| payload | jsonb | Indexing: `branch`, plus the `commit_sha` that the job resolves. Question: none beyond the run |
| dedupe_key | text | Indexing: `index:{repository_id}:{branch}`. Question: `run:{run_id}` |
| status | text | See the state machine below |
| attempt | integer | 0 before the first claim |
| max_attempts | integer | 3 |
| fencing_token | bigint | Incremented at every claim |
| lease_expires_at | timestamptz | |
| run_after | timestamptz | Earliest time the job can be claimed |
| deadline_at | timestamptz | Set at first start: +15 min for indexing, +3 min for a question |
| error_code, error_message | text | Safe for users |
| error_retryable | boolean | |
| created_by | uuid | FK users |
| created_at, started_at, finished_at | timestamptz | |

Constraints and indexes:

- Unique `(workspace_id, kind) WHERE status = 'running'`: one running job per kind per workspace
  (FR-027).
- Unique `(workspace_id, dedupe_key) WHERE status IN ('queued', 'running', 'retry_wait')`.
- Index on `(status, run_after)` for claiming.

State machine (from the design document):

```text
queued -> running -> succeeded
             |
             +-> retry_wait -> running   (transient failure, attempts left, deadline not passed)
             +-> failed                  (permanent failure, attempts exhausted, or deadline passed)
             +-> canceled                (repository disconnected)
```

- A `running` job whose lease expired can be claimed again, and the claim increments `attempt`
  and `fencing_token`.
- Permanent failures fail immediately (FR-030): `access_denied`, `branch_not_found`,
  `repository_empty`, `limit_exceeded`, `unsupported_input`, `citation_validation_failed`, and `model_refused`.

### job_events

| Field | Type | Rules |
| --- | --- | --- |
| job_id | uuid | PK part, FK jobs |
| seq | integer | PK part; increases monotonically per job |
| event_type | text | `queued`, `stage_started`, `stage_completed`, `retry_scheduled`, `succeeded`, `failed`, or `canceled` |
| stage | text | Indexing: `resolving_commit`, `fetching_source`, `filtering`, `parsing`, `building_search_index`, `embedding_docs`, `publishing`. Question: `retrieving_evidence`, `generating_answer`, `validating_citations`, `publishing` |
| message | text | User-facing, contains no source text |
| data | jsonb | Safe counters, for example `{"files": 812}` |
| created_at | timestamptz | |

## Questions and answers

Terminology: the spec's question and answer are stored together as an analysis run with
`kind = 'repository_qa'`. The API path is `/v1/analysis-runs`, and the web route is `/answers/{id}`.
Later analysis kinds reuse the same table.

### analysis_runs

| Field | Type | Rules |
| --- | --- | --- |
| id | uuid | PK |
| workspace_id, repository_id, snapshot_id | uuid | Composite FKs; the snapshot is pinned at submission (FR-022) |
| commit_sha, index_version | text | Copied from the snapshot at submission |
| kind | text | `repository_qa`, the only kind in this increment |
| question | text | 1 to 2,000 characters after trimming |
| job_id | uuid | FK jobs |
| quality_state | text | `answered` or `insufficient_evidence`; null until it succeeds |
| result | jsonb | Validated answer: `summary`, `claims[]` with evidence labels, `gaps[]` |
| model, thinking_level, prompt_version | text | Configuration pinned per run |
| usage | jsonb | Model calls and input, output, thinking, and cached tokens |
| created_by | uuid | FK users |
| created_at, completed_at | timestamptz | |
| expires_at | timestamptz | `created_at + 30 days` (FR-035) |

The execution state comes from the linked job. The quality state is stored separately, so a run
can succeed with `insufficient_evidence` (FR-021). A provider failure is an execution failure and
never a quality outcome (FR-024).

### evidence_items

| Field | Type | Rules |
| --- | --- | --- |
| id | uuid | PK |
| analysis_run_id | uuid | FK analysis_runs, on delete cascade |
| label | text | `E1` to `E12`; unique (analysis_run_id, label) |
| source_type | text | `symbol`, `code`, or `doc` |
| path | text | |
| commit_sha | text | Equal to the run's commit |
| start_line, end_line | integer | Within the file's `line_count`; checked at write time |
| excerpt | text | Exact lines from the pinned snapshot |
| excerpt_sha256 | bytea | |
| rank | integer | Fused retrieval rank |

Evidence items and the run's `result` are written in the same fenced transaction, so retried
attempts never leave duplicate evidence. Citation views are built from these rows (FR-020).

## Supporting records

### idempotency_records

| Field | Type | Rules |
| --- | --- | --- |
| workspace_id, route, key | uuid, text, text | PK |
| payload_sha256 | bytea | Same key with a different payload returns 409 |
| response_status | integer | |
| response_body | jsonb | Replayed for a duplicate request (FR-029) |
| created_at, expires_at | timestamptz | `expires_at = created_at + 24 hours` |

### usage_counters

| Field | Type | Rules |
| --- | --- | --- |
| workspace_id, usage_date | uuid, date | PK; `usage_date` is the UTC calendar day |
| questions_count | integer | Incremented in the transaction that creates the run, only while below `daily_question_limit`, default 20 (FR-028) |

A question counts when it is accepted, whatever its outcome. The allowance resets at 00:00 UTC.

### audit_events

| Field | Type | Rules |
| --- | --- | --- |
| id | bigint | PK |
| workspace_id, actor_user_id | uuid | Nullable for failed sign-ins |
| action | text | `sign_in`, `sign_out`, `repository_connect`, `repository_disconnect`, `question_submit`, or `access_denied` |
| resource_type, resource_id | text | |
| outcome | text | `success`, `denied`, or `failure` |
| request_id | text | |
| detail | jsonb | No secrets and no source text |
| created_at | timestamptz | |

## Retention summary

| Data | Removed by the maintenance pass |
| --- | --- |
| Disconnected repository's snapshots, files, chunks, runs, and evidence | Within 24 hours of `deleted_at` |
| Analysis runs and evidence | After `expires_at` (30 days) |
| Ready snapshots that are neither active nor referenced by an unexpired run | 14 days after they stop being active |
| `failed` and `discarded` snapshots | After 1 day |
| Sessions | After expiry or revocation |
| Idempotency records | After `expires_at` |
