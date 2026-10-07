---

description: "Task list for Automatic Re-indexing and Access Revocation"
---

# Tasks: Automatic Re-indexing and Access Revocation

**Input**: Design documents from `specs/002-push-reindexing/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/github-webhooks.md,
contracts/http-api.md, quickstart.md

**Tests**: Included. The project's tested-core rule requires automated tests for core logic, and
a story is not done until its tests pass. Within each story, write the tests first and confirm
that they fail before implementing.

**Organization**: Tasks are grouped by user story, so each story can be implemented and tested as
an increment.

**Environment note**: The development GitHub App from 001 is registered and installed. T003 turns
on its webhook, and it is needed only for the real-mode scenarios in T051. Everything else runs
against the fake gateway.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: The user story the task belongs to (US1 to US3)
- Paths are relative to the repository root: `backend/src/codeatlas/`, `backend/tests/`,
  `frontend/src/`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Configuration for the webhook secret

- [X] T001 Add the webhook secret setting:
  - Add an empty `GITHUB_WEBHOOK_SECRET=` line to .env.example.
  - Add `github_webhook_secret: str = ""` to backend/src/codeatlas/config.py.
  - Extend the settings validator: raise when `env == "production"` and the secret is empty
    (research R2).
  - Add unit tests to backend/tests/unit/test_config.py: production without a secret fails;
    development with an empty secret loads.
- [X] T002 [P] Generate a per-run webhook secret in .github/workflows/ci.yml:
  - In the `backend` and `e2e` jobs, before tests run, add
    `echo "GITHUB_WEBHOOK_SECRET=$(openssl rand -hex 32)" >> "$GITHUB_ENV"`.
  - No secret value is committed.
- [X] T003 Developer action, needed only before T051 (quickstart.md, "GitHub App changes"):
  - In the development GitHub App, turn the webhook on. Its URL is a Smee channel that forwards
    to `http://localhost:8000/webhooks/github`, and its secret is a random high-entropy string.
  - Subscribe the App to `Push`. Leave its permissions unchanged.
  - Put the same secret in `.env` as `GITHUB_WEBHOOK_SECRET`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Schema, error classification, fake-gateway switches, webhook verification and
parsing, the endpoint skeleton, and access-state transitions

The endpoint is built here with every event treated as `ignored`. Each story then registers its
own event handlers in backend/src/codeatlas/workspace/sync.py.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

### Tests for the foundation ⚠️ (write first, confirm they fail)

- [X] T004 [P] Add webhook test helpers:
  - Create backend/tests/webhooks.py with these builders, mirroring contracts/github-webhooks.md:
    - `push_payload(github_repository_id, after, *, ref="refs/heads/main", default_branch="main", private=False, deleted=False)`;
    - `installation_payload(action, installation_id)`;
    - `installation_repositories_payload(action, installation_id, removed_ids)`;
    - `authorization_revoked_payload(github_user_id)`.
  - Add `send_delivery(client, event, payload, *, delivery_id=None, secret=None, raw_body=None)`.
    It signs the body with HMAC-SHA256, sets `X-GitHub-Event`, `X-GitHub-Delivery` (a new UUID
    when none is given), and `X-Hub-Signature-256`, and sends no session cookie and no `Origin`.
  - Add an autouse `webhook_secret` fixture in backend/tests/conftest.py that sets
    `GITHUB_WEBHOOK_SECRET` to a test value and clears the settings cache.
- [X] T005 [P] Unit tests for signature checks in backend/tests/unit/test_webhook_signature.py:
  - A correct `sha256=<hex>` header is accepted.
  - These are rejected:
    - a wrong secret;
    - a body changed by one byte;
    - a missing header;
    - a header without the `sha256=` prefix;
    - a header with non-hex digits;
    - a request that carries only the SHA-1 `X-Hub-Signature`.
  - An empty configured secret rejects every request.
- [X] T006 [P] Unit tests for payload parsing in backend/tests/unit/test_webhook_events.py:
  - A push to `refs/heads/<default_branch>` parses to a push event carrying the repository ID,
    default branch, `after`, and `private`.
  - These are ignored with a reason:
    - a push to another branch (`not_default_branch`);
    - a push to `refs/tags/...` (`tag`);
    - a push with `deleted: true` (`branch_deleted`).
  - `installation.deleted` gives reason `app_uninstalled`, and `installation.suspend` gives
    `app_suspended`.
  - `installation.created`, `unsuspend`, and `new_permissions_accepted` are ignored.
  - `installation_repositories.removed` lists the removed repository IDs; `added` is ignored.
  - `github_app_authorization.revoked` carries `sender.id`.
  - `ping` and unknown events are ignored.
  - A push missing `repository.id` or `after` is invalid.
- [X] T007 [P] Unit tests for GitHub error classification (research R4) in
  backend/tests/unit/test_github_client_errors.py, using `httpx.MockTransport`:
  - A 401 on `GET /repositories/{id}`, `GET /user/installations`, or the installation repository
    listing raises `UserAuthorizationInvalid`. So does a refused token refresh
    (`bad_refresh_token`).
  - A 401 on `GET /repos/{owner}/{repo}/installation` or
    `POST /app/installations/{id}/access_tokens` raises `AppCredentialsRejected`.
  - A 404, or a plain 403, on `GET /repositories/{id}` raises `GitHubNotFound`.
  - A plain 403 on installation-token calls raises `GitHubAccessDenied`, not
    `UserAuthorizationInvalid`.
  - A 429, a 5xx, or a rate-limited 403 raises `GitHubUnavailable`.
  - `UserAuthorizationInvalid` is a subclass of `GitHubAccessDenied`.
- [X] T008 [P] Unit tests for the new fake-gateway switches in
  backend/tests/unit/test_fake_github.py:
  - `push(SAMPLE_APP_ID)` moves the default branch to the next commit and returns its SHA.
  - `rename_default_branch(SAMPLE_APP_ID, "trunk")` renames the default branch:
    `get_repository` reports `default_branch="trunk"`, `resolve_commit(..., "trunk")` returns the
    old `main` head, and `main` raises `BranchNotFound`.
  - `suspend(SAMPLE_APP_ID)` drops the owner's installation from `list_installation_ids`, and
    `resolve_commit` then raises `GitHubAccessDenied`.
  - `remove_from_installation(SAMPLE_APP_ID)` makes `get_installation_id` raise
    `GitHubNotFound`.
  - `revoke_authorization("octocat")` makes octocat's token and refresh raise
    `UserAuthorizationInvalid`.
  - `make_private(SAMPLE_APP_ID)` makes `get_repository` report `private=True`.
  - `set_unavailable(True)` makes every gateway method raise `GitHubUnavailable`.
  - `reset()` undoes all of these.
- [X] T009 [P] Integration tests for the endpoint skeleton (contracts/github-webhooks.md) in
  backend/tests/integration/test_webhooks.py:
  - A signed `ping` returns 202 `{"outcome": "ignored"}` and stores a `webhook_deliveries` row with
    outcome `ignored`.
  - The same delivery ID again returns 202 `duplicate` and creates no second row.
  - A missing or wrong signature returns 401 `invalid_signature`. It writes exactly one
    `webhook_rejected` audit event with a null workspace and no delivery row (SC-004).
  - A body over 25 MiB returns 413 `payload_too_large`.
  - A missing `X-GitHub-Delivery` or `X-GitHub-Event` header, or a body that is not JSON, returns
    400 `invalid_delivery`.
  - No session cookie or `Origin` header is required.
  - With an empty secret, every delivery returns 401.
- [X] T010 [P] Integration tests for access-state transitions (data-model.md state machine) in
  backend/tests/integration/test_access_state.py:
  - `mark_access_lost` sets the state, reason, and `access_lost_at`. It cancels `queued` and
    `retry_wait` jobs with the message "Canceled because access to the repository was lost.", and
    records `repository_access_lost` with a null actor.
  - A second loss keeps the first `access_lost_at`.
  - `pause` on a lost repository changes nothing.
  - `restore_access` clears the reason and `access_lost_at`, and records
    `repository_access_restored`.
  - `resume` records `automatic_updates_resumed` with the given actor.
  - `ensure_readable` raises 403 `repository_access_lost` with `details` `reason`, `lost_at`, and
    `purge_after = lost_at + 7 days`.
  - The database rejects rows that break the access-state check constraints.

### Implementation for the foundation

- [X] T011 Add Alembic migration backend/alembic/versions/0002_push_reindexing.py and update
  backend/src/codeatlas/models.py (data-model.md):
  - **repositories**: add the following columns.
    - `access_state` text, not null, default `'active'`.
    - `access_reason` text, `access_lost_at` timestamptz, `access_checked_at` timestamptz,
      `latest_push_sha` text, and `latest_push_at` timestamptz.
    - `latest_push_job_id` uuid, FK jobs, on delete set null.
  - **repositories constraints**, verbatim from the data model:
    - `access_state IN ('active', 'paused', 'access_lost')`;
    - `(access_state = 'active') = (access_reason IS NULL)`;
    - `(access_state = 'access_lost') = (access_lost_at IS NOT NULL)`.
  - **jobs**: add `trigger` text, not null, default `'user'`, with check
    `trigger IN ('user', 'push', 'check')`.
  - **Dedupe index**: replace `uq_jobs_active_dedupe_key` with `uq_jobs_waiting_dedupe_key` on
    `(workspace_id, dedupe_key) WHERE status IN ('queued', 'retry_wait')`.
  - **webhook_deliveries**: create the table.
    - `delivery_id` text PK, `event` text, `action` text, `github_installation_id` bigint,
      `github_repository_id` bigint, `outcome` text, `detail` jsonb, and `received_at`
      timestamptz.
    - Outcome check: `outcome IN ('processed', 'ignored')`.
    - An index on `received_at`.
  - **Audit actions**: extend `AUDIT_ACTIONS` and its check constraint with `webhook_rejected`,
    `repository_access_lost`, `repository_access_restored`, `automatic_updates_paused`, and
    `automatic_updates_resumed`.
  - **Constants**: add `ACCESS_STATES`, `JOB_TRIGGERS`, and `WAITING_JOB_STATUSES`.
  - **Downgrade**: reverses every change.
  - **Verify**: `alembic upgrade head`, then `downgrade -1`, then `upgrade head`, against the local
    database.
- [X] T012 Classify GitHub failures (research R4):
  - In backend/src/codeatlas/github/gateway.py, add `UserAuthorizationInvalid(GitHubAccessDenied)`
    and `AppCredentialsRejected(GitHubError)`.
  - In backend/src/codeatlas/github/client.py, `_check` takes the class to raise on 401:
    - user-token calls pass `UserAuthorizationInvalid`;
    - App-JWT and installation-token minting calls pass `AppCredentialsRejected`;
    - a rejected token request raises `UserAuthorizationInvalid`.
  - In backend/src/codeatlas/auth/github_login.py, `get_user_token` raises
    `UserAuthorizationInvalid` when there is no credential or no refresh token. The decryption
    path keeps its 001 behavior.
  - Make T007 pass. The existing 001 tests stay green.
- [X] T013 Add the switches from T008 to backend/src/codeatlas/github/fake.py:
  - `push`, `rename_default_branch`, `suspend`, `remove_from_installation`,
    `revoke_authorization`, `make_private`, and `set_unavailable`. `reset()` restores all of them.
  - `push` returns the new SHA, so tests can build matching payloads.
  - Make T008 pass. Depends on T012.
- [X] T014 [P] Implement backend/src/codeatlas/github/webhooks.py, with no database or framework
  imports:
  - `MAX_BODY_BYTES = 25 * 1024 * 1024`.
  - `verify_signature(secret: str, body: bytes, header: str | None) -> bool`, using
    `hmac.compare_digest`.
  - Frozen dataclasses `PushEvent`, `InstallationLost`, `RepositoriesRemoved`,
    `AuthorizationRevoked`, `Ignored(reason)`, and the exception `InvalidDelivery`.
  - `parse(event: str, payload: Mapping[str, Any])`, following the rules tested in T006.
  - Make T005 and T006 pass.
- [X] T015 Implement the endpoint skeleton:
  - Create backend/src/codeatlas/api/routes/webhooks.py with `POST /webhooks/github`:
    1. Read the body from `request.stream()` up to `MAX_BODY_BYTES`, else 413
       `payload_too_large`.
    2. Verify the signature, else 401 `invalid_signature`. Record `webhook_rejected` in its own
       transaction, with detail `{"event": <header>, "reason": "signature_missing" | "signature_mismatch"}`.
    3. Check the headers and JSON, else 400 `invalid_delivery`.
    4. Insert the delivery row with `ON CONFLICT (delivery_id) DO NOTHING`; on a conflict, return
       202 `duplicate`.
    5. Call `sync.apply_event(db, parsed)`.
    6. Store the outcome and safe `detail`, commit, and return 202 `{"outcome": ...}`.
  - Create backend/src/codeatlas/workspace/sync.py with `apply_event`, which dispatches on the
    parsed type through a handler registry. Unregistered types return `ignored`.
  - In backend/src/codeatlas/api/app.py, include the router, and make the Origin check skip paths
    that start with `/webhooks/`.
  - Never log request bodies.
  - Make T009 pass. Depends on T011 and T014.
- [X] T016 Implement backend/src/codeatlas/workspace/access.py:
  - `GRACE_PERIOD = timedelta(days=7)`.
  - `mark_access_lost(db, repository, reason, *, trigger)`:
    - locks the row and keeps an existing `access_lost_at`;
    - sets the state and reason, and sets `access_checked_at` when `trigger` is a run trigger;
    - cancels waiting jobs and records `repository_access_lost`.
  - `restore_access(db, repository, *, trigger)`.
  - `pause(db, repository, reason)`: no change when the repository is lost; otherwise records
    `automatic_updates_paused`.
  - `resume(db, repository, *, via, actor_user_id)`.
  - `ensure_readable(repository)`: raises the 403 error from contracts/http-api.md.
  - `purge_after(repository)`.
  - In backend/src/codeatlas/jobs/queue.py, give `cancel_for_repository` a `message` parameter.
    Its default stays the 001 disconnect message.
  - Make T010 pass. Depends on T011.

**Checkpoint**: The schema migrates, deliveries are verified and recorded, and access-state
transitions are tested. User story work can begin.

---

## Phase 3: User Story 1 - Keep a repository current after pushes (Priority: P1) 🎯 MVP

**Goal**: A push to a connected repository's default branch is indexed and becomes the default
version, without user action.

**Independent Test**: Connect `sample-app`, call `fake.push(SAMPLE_APP_ID)`, and send the
matching signed push delivery. After running the worker, the second commit is the default
version, with trigger `push`, and its renamed file is searchable.

### Tests for User Story 1 ⚠️ (write first, confirm they fail)

- [X] T017 [P] [US1] Integration tests for coalescing (research R3) in
  backend/tests/integration/test_coalescing.py:
  - With no jobs, `request_automatic_run(trigger="push", pushed_commit_sha=X)` creates a `queued`
    job with trigger `push`, `created_by` null, and `payload.pushed_commit_sha = X`.
  - A second request while that job is queued returns the same job, updated to the newer pushed
    SHA.
  - With a running job whose `payload.commit_sha` equals the pushed SHA, no job is created.
  - With a running job on an older commit, exactly one `queued` job is created, and a third
    request returns it.
  - A waiting `check` job takes trigger `push` when a push merges into it, and trigger `user` when
    a manual re-index merges into it.
  - A manual re-index while a job is running returns the running job (the 001 contract).
  - The database allows one running and one queued job with the same key, and rejects two queued
    ones.
- [X] T018 [P] [US1] Integration tests for pushes in
  backend/tests/integration/test_push_reindexing.py, using T004's helpers and `run_worker_once`:
  - **Push indexed** (US1-1, FR-001, FR-009):
    - a signed push for `fake.push(SAMPLE_APP_ID)` returns 202 `processed` and sets
      `latest_push_sha`, `latest_push_at`, and `latest_push_job_id`;
    - after the worker runs, the second commit is active and the version's `trigger` is `push`;
    - `GET /v1/repositories/{id}` returns `latest_push.job.trigger == "push"`.
  - **Other refs** (US1-2): a push to `refs/heads/feature` or to a tag is `ignored` and creates no
    job.
  - **Burst** (US1-3, SC-002): 20 distinct push deliveries while the first run is running create
    at most 2 runs in total, and the last pushed commit ends active.
  - **Push during a run** (US1-4): a push while a run is indexing an older commit leads to exactly
    one follow-up run, which indexes the newest head.
  - **Duplicates** (US1-5, SC-002): sending one delivery 100 times creates 1 job.
  - **Failure** (US1-6): with `max_files_per_snapshot` lowered so that the second commit fails
    with `limit_exceeded`, the first commit stays active, and `latest_push.job.error.code` is
    `limit_exceeded`.
  - **Pinned answers** (US1-7): an answer pinned to the first commit still shows that commit after
    the push.
  - **Unconnected repository** (FR-010): a push for a repository no workspace connected is
    `ignored`, and the fake's call counter shows no GitHub call.
  - **Two workspaces** (FR-010): `sample-app` connected by `octocat` and by `hubot` gets one job
    and one snapshot per workspace.
  - **Manual merge** (FR-005): a manual re-index while an automatic run is waiting returns that
    job.
  - **Default branch renamed** (spec edge case): after `fake.rename_default_branch(SAMPLE_APP_ID, "trunk")`
    and `fake.push(SAMPLE_APP_ID)`, a push delivery with `ref: refs/heads/trunk` and
    `default_branch: trunk`:
    - sets the repository's `default_branch` to `trunk`;
    - queues a run on `trunk`, which publishes the new commit.

    A later push to `refs/heads/main` is `ignored`.
  - **Force push**: a push back to an already indexed commit reuses that version and makes it
    active.
  - **Owner access** (FR-011): an automatic run, with `created_by` null, verifies the workspace
    owner's access.
- [X] T019 [P] [US1] Fault-injection ordering tests (SC-003, FR-006) in
  backend/tests/integration/test_run_ordering.py:
  - A run resolves commit X, then stalls past its lease. A push of Y creates a waiting job.
  - The stalled job is reclaimed, and the new attempt resolves Y. The stale attempt's publish is
    rejected, and its snapshot ends `discarded`.
  - The waiting job never starts while another indexing job in the workspace is running.
  - In every interleaving tried, the active version ends at the last pushed commit.
- [X] T020 [P] [US1] Retention tests (FR-019, FR-020, SC-010) in
  backend/tests/integration/test_maintenance.py:
  - With 7 ready versions v1 to v7, where v7 is active, a pass removes v1 and v2 and keeps v3 to
    v7.
  - A version that an unexpired answer references is kept, however many newer versions exist.
  - `webhook_deliveries` rows older than 14 days are removed, and newer rows are kept.

### Implementation for User Story 1

- [X] T021 [US1] Implement coalescing in backend/src/codeatlas/jobs/queue.py:
  - `enqueue` gains `trigger: str = "user"`. When it finds a waiting `check` job, it gives that
    job its own trigger.
  - New `request_automatic_run(db, *, repository, branch, trigger, pushed_commit_sha=None) -> Job`,
    following research R3, with `created_by=None`.
  - `Claim` and `JobContext` carry `trigger`.
  - Make T017 pass.
- [X] T022 [US1] Implement the push handler in backend/src/codeatlas/workspace/sync.py, following
  contracts/github-webhooks.md `push`:
  - For each connected repository with the pushed GitHub ID: set `latest_push_sha` and
    `latest_push_at`, and refresh `default_branch` from the payload.
  - Skip queuing when `access_state == "paused"`. Otherwise call `request_automatic_run(trigger="push")`
    and set `latest_push_job_id`.
  - Return `processed` with detail `{"ref", "after", "repositories": n}`, or `ignored` with reason
    `not_connected`.
  - Register the handler for `PushEvent`.
- [X] T023 [US1] Update backend/src/codeatlas/ingestion/pipeline.py:
  - `_resolve` loads the workspace owner (`Membership.role == "owner"`) and uses their token,
    instead of `ctx.created_by` (research R5).
  - Audit events for automatic runs have a null actor and include the trigger in `detail`.
  - `push` and `user` runs keep 001's reuse behavior for an already indexed commit.
  - Make T018 and T019 pass.
- [X] T024 [US1] Add retention to backend/src/codeatlas/jobs/maintenance.py:
  - Delete ready snapshots that meet all of these: not active; not referenced by an unexpired run;
    and either `row_number() OVER (PARTITION BY repository_id ORDER BY ready_at DESC) > 5` or
    created more than 14 days ago.
  - Delete `webhook_deliveries` with `received_at` older than 14 days.
  - Add the counts to `MaintenanceResult`.
  - Make T020 pass.
- [X] T025 [US1] Expose triggers and pushes in the API (contracts/http-api.md):
  - In backend/src/codeatlas/api/routes/repositories.py:
    - `RepositoryOut.latest_push` is `{commit_sha, received_at, job: {id, status, trigger, error} | null}`
      or null;
    - `LatestJobOut.trigger` is added;
    - `SnapshotOut.trigger` is read from the snapshot's job.
  - Add `trigger` to the job response in backend/src/codeatlas/api/routes/jobs.py.
- [X] T026 [US1] Show pushes and triggers in the frontend:
  - Regenerate frontend/src/lib/api/schema.d.ts (`npm run gen:api`) and update the types in
    frontend/src/lib/api/repositories.ts.
  - Create frontend/src/components/AutomaticUpdatesStatus.tsx. It shows the latest push's short
    SHA, when it was received, and its run status, or "No pushes received yet". When that run
    failed, it shows `latest_push.job.error.message` and says that the previous version is still
    the default (US1-6).
  - Render it in frontend/src/app/repositories/[id]/repository-detail.tsx.
  - Label each version in frontend/src/components/SnapshotSelector.tsx, and the latest run,
    "Indexed by you", "After a push", or "Daily check" by trigger.
  - `npm run lint`, `npm run typecheck`, and `npm run build` pass.

**Checkpoint**: Pushes keep repositories current. Access loss still behaves as in 001 (the run
fails, and the data stays readable) until User Story 2.

---

## Phase 4: User Story 2 - Stop serving a repository after GitHub access is lost (Priority: P1)

**Goal**: Once access loss is detected, by a run or an installation notification, the
repository's content is denied immediately. It is restored if access returns within 7 days, and
purged otherwise.

**Independent Test**: Connect and index `sample-app`, then send a signed `installation.deleted`
delivery for installation 5001. Every content endpoint returns 403 `repository_access_lost`, and
the repository shows `state: access_lost`. Restore the fake installation and re-index: the earlier
versions are readable again without re-indexing.

### Tests for User Story 2 ⚠️ (write first, confirm they fail)

The tests start runs with manual re-index requests, so this story does not depend on User Story
1.

- [X] T027 [P] [US2] Integration tests in backend/tests/integration/test_access_revocation.py:
  - **Detection by a run** (US2-1, FR-011):
    - Each fake switch leads the run to fail with `access_denied` before any `open_tarball` call
      (per the call counter). The repository becomes `access_lost` with the matching reason, and
      `repository_access_lost` is recorded:
      - `revoke_access` on the private fixture gives `repository_not_visible`;
      - `uninstall` and `remove_from_installation` give `app_not_installed`;
      - `suspend` gives `installation_not_accessible`.
    - `access_checked_at` is set.
  - **Notifications** (US2-2, SC-005): each notification marks the repository lost within the
    webhook request itself, with no worker run, and cancels its waiting jobs:
    - `installation.deleted` gives `app_uninstalled`;
    - `installation.suspend` gives `app_suspended`;
    - `installation_repositories.removed` gives `repository_removed_from_installation`.

    Repositories under other installation IDs, or not listed as removed, are untouched.
  - **No publish after loss** (US2-3, SC-007): loss marked while a run is between `filtering` and
    `publishing` (injected through a fake hook) leaves the snapshot `discarded`, the job
    `canceled`, and the active version unchanged. The same holds for the reuse path.
  - **No false positives** (US2-4, FR-013, SC-008):
    - `set_unavailable(True)` gives a retryable `github_unavailable` failure, with `access_state`
      still `active` and `access_checked_at` unchanged.
    - A monkeypatched `AppCredentialsRejected` gives `github_app_misconfigured`, with no state
      change.
    - `UserAuthorizationInvalid` gives `github_sign_in_required`, with no loss.
  - **Read denial** (FR-014):
    - These endpoints return 403 `repository_access_lost` with `reason`, `lost_at`, and
      `purge_after`:
      - `GET /v1/repositories/{id}/snapshots`;
      - snapshot detail, coverage, tree, and file;
      - `POST /v1/search`;
      - analysis-run submit, list, and get;
      - job get and job events.
    - `GET /v1/repositories` and `GET /v1/repositories/{id}` return 200 with
      `state: access_lost` and the `access` object.
    - The other user gets 404 for all of them.
  - **Restoration** (US2-5, SC-011): after `reset()` restores access, a manual re-index sets the
    state back to `active` and records `repository_access_restored`. The earlier versions and
    answers are readable without building a new snapshot, because the run reuses the existing
    one.
  - **Push to a lost repository** (spec edge case, US2-1). Needs the push handler from T022:
    - With access still revoked in the fake, a push delivery queues a `push` run. The run fails
      with `access_denied` before any `open_tarball` call, the repository stays lost, and
      `access_lost_at` keeps its first value.
    - After `reset()` restores access, the next push delivery queues a run that records
      `repository_access_restored` and publishes the pushed commit with trigger `push`.
  - **First detection time**: a second detection keeps the first `access_lost_at`.
- [X] T028 [P] [US2] Grace-expiry tests (US2-6, SC-011) in
  backend/tests/integration/test_maintenance.py:
  - A repository lost for 7 days and 1 minute is tombstoned, its waiting jobs are canceled, and
    `repository_disconnect` is recorded with a null actor and `{"reason": "access_lost"}`. It is
    purged in the same pass.
  - A repository lost for 6 days is untouched.

### Implementation for User Story 2

- [X] T029 [US2] Classify access results in the pipeline (research R4):
  - In backend/src/codeatlas/github/gateway.py, add `AccessCheckFailed(GitHubNotFound)` with a
    `reason`. `verify_access` raises it with these reasons:
    - `repository_not_visible` when `get_repository` raises `GitHubNotFound`;
    - `app_not_installed` when `get_installation_id` raises `GitHubNotFound`;
    - `installation_not_accessible` when the membership check fails.

    Connecting still maps it to 404.
  - In backend/src/codeatlas/ingestion/pipeline.py:
    - `AccessCheckFailed`, and `GitHubNotFound` or `GitHubAccessDenied` from branch resolution or
      the tarball (`installation_cannot_read`), call `mark_access_lost`, then fail the job with
      `access_denied`.
    - `UserAuthorizationInvalid` fails the job with `github_sign_in_required` (permanent,
      retryable) and no state change.
    - `AppCredentialsRejected` fails the job with `github_app_misconfigured` (not permanent).
    - A passed check sets `access_checked_at` and, for a lost repository, calls `restore_access`.
  - Depends on T023, since both edit pipeline.py.
- [X] T030 [US2] Guard publishing in backend/src/codeatlas/ingestion/pipeline.py:
  - `_publish` and `_reuse` treat `access_state == "access_lost"` like a tombstone: the snapshot
    is `discarded`, and the job is canceled with "Access to the repository was lost."
  - Make the publish part of T027 pass.
- [X] T031 [US2] Implement installation handlers in backend/src/codeatlas/workspace/sync.py,
  following contracts/github-webhooks.md:
  - `InstallationLost` marks every connected repository with that `github_installation_id`.
  - `RepositoriesRemoved` marks the listed IDs under that installation.
  - Both pass `trigger="notification"` and return `processed` when any repository matched, else
    `ignored`.
  - Register both handlers.
- [X] T032 [US2] Deny reads of lost repositories:
  - In backend/src/codeatlas/workspace/repositories.py:
    - `get_scoped` gains `content: bool = True` and calls `ensure_readable` when it is true.
    - Repository detail, the repository list, re-index, and disconnect pass `content=False`.
    - `get_scoped_snapshot` calls `ensure_readable`.
  - Apply the same check to job lookups in backend/src/codeatlas/api/routes/jobs.py.
  - Apply it to analysis runs in backend/src/codeatlas/qa/runs.py (`get_scoped` and submission)
    and backend/src/codeatlas/api/routes/analysis_runs.py (listing).
  - Make the read-denial part of T027 pass.
- [X] T033 [US2] Show access in the repository API:
  - In backend/src/codeatlas/workspace/repositories.py, `RepositoryState` gains `access_lost`, and
    `derive_state` returns it first.
  - In backend/src/codeatlas/api/routes/repositories.py, `RepositoryOut.access` is
    `{state: "verified" | "lost" | "unknown", reason, checked_at, lost_at, purge_after}`.
- [X] T034 [US2] Expire the grace period in backend/src/codeatlas/jobs/maintenance.py:
  - Before the purge step, tombstone repositories with `access_state = 'access_lost'` and
    `access_lost_at < now - 7 days`.
  - Cancel their waiting jobs, and record `repository_disconnect` with a null actor and
    `{"reason": "access_lost"}`.
  - Make T028 pass.
- [X] T035 [US2] Show access loss in the frontend:
  - In frontend/src/components/RepositoryStateBadge.tsx, add the `access_lost` state, labeled
    "Access lost".
  - Create frontend/src/components/RepositoryAccessPanel.tsx. For a verified repository, it shows
    the last check time. For a lost one, it shows:
    - a plain-English reason, mapped from each reason code;
    - when access was lost, and the purge date;
    - the guidance "Restore access to the repository for the CodeAtlas GitHub App, then choose
      Re-index".
  - In frontend/src/app/repositories/[id]/repository-detail.tsx, render the panel. Hide the
    browse, search, and ask actions while access is lost.
  - While `state` is `access_lost`, repository-detail.tsx does not render `JobProgress`, because
    job reads return 403 by design (FR-014). After a re-index request, it polls
    `GET /v1/repositories/{id}` every 1.5 seconds and shows "Checking access…" while
    `latest_indexing_job` is active. Once `state` is no longer `access_lost`, normal job progress
    resumes. If the run ends and the repository is still lost, it shows
    `latest_indexing_job.error.message`.
  - Add `isAccessLost(error)` to frontend/src/lib/api/client.ts. On 403 `repository_access_lost`,
    frontend/src/components/SnapshotFrame.tsx and frontend/src/app/answers/[id]/answer-detail.tsx
    show a notice linking back to the repository page.
  - `npm run lint`, `npm run typecheck`, and `npm run build` pass.

**Checkpoint**: User Stories 1 and 2 together are the releasable increment. Pushes keep
repositories current, and lost access stops serving them.

---

## Phase 5: User Story 3 - Catch missed changes with a daily check (Priority: P2)

**Goal**: A daily check of every repository catches missed pushes and silent access loss. Lapsed
authorization and private-repository disclosure pause automatic updates instead of breaking them.

**Independent Test**: With an injected clock, run `schedule_checks` and the worker. A repository
whose owner lost access without a notification becomes `access_lost`. A repository with an
undelivered push indexes the new head with trigger `check`. An up-to-date repository's run
finishes "Already up to date".

Depends on User Story 1 (`request_automatic_run`) and User Story 2 (loss marking in the pipeline).

### Tests for User Story 3 ⚠️ (write first, confirm they fail)

- [X] T036 [P] [US3] Integration tests in backend/tests/integration/test_daily_check.py, with
  `now` injected:
  - **Scheduling**:
    - `schedule_checks` enqueues `check` jobs (`created_by` null, default branch) for repositories
      whose `access_checked_at` is null or older than 20 hours.
    - It skips paused repositories, tombstoned repositories, and repositories with a waiting or
      running indexing job.
    - It includes lost repositories.
    - A second runner skips while the advisory lock is held.
  - **Up to date** (US3-3): when the head already has a ready version, the job succeeds with
    "Already up to date". The active pointer is unchanged, even when the active version is on
    another branch, and only `access_checked_at` moves.
  - **Missed push** (US3-2, FR-018, SC-009): `fake.push` without a delivery leads the check to
    index the new head, with trigger `check`.
  - **Silent loss** (US3-1, SC-006): `revoke_access` without a notification leads the check to
    mark the repository lost.
  - **Repeated permanent failure**: when the latest finished run failed with `limit_exceeded` at
    the same commit, the check does not index again.
  - **Transient failure**: it leaves `access_checked_at` unchanged, and the next pass enqueues
    again.
  - **Timeline**: checked at t0, access revoked at t0 + 1 h, and passes every 30 minutes on the
    injected clock. Loss is detected before t0 + 24 h.
- [X] T037 [P] [US3] Integration tests in backend/tests/integration/test_pauses.py:
  - **Lapsed authorization in a run** (US3-4): after `revoke_authorization("octocat")`, a run:
    - fails with `github_sign_in_required`;
    - pauses the repository with `sign_in_required`;
    - deletes octocat's `github_credentials` row, while existing sessions stay valid;
    - records `automatic_updates_paused`.
  - **Authorization revoked notification**: a signed `github_app_authorization.revoked` pauses
    every active repository in octocat's workspace and deletes the credential. Lost repositories
    stay lost.
  - **Push while paused**: the push is recorded, `latest_push_job_id` is null, no job is created,
    and `schedule_checks` skips the repository.
  - **Sign-in resumes** (FR-016): signing in again sets the paused repositories back to `active`,
    records `automatic_updates_resumed` with octocat as the actor, and queues one `check` job per
    repository.
  - **Became private** (FR-017): `make_private(SAMPLE_APP_ID)` on a repository with no acceptance
    makes the run fail with `external_processing_not_accepted`, with zero `open_tarball` calls,
    and pauses the repository with `external_processing_not_accepted`.
  - **Acceptance**: `POST /v1/repositories/{id}/index` without acceptance returns 422
    `external_processing_not_accepted`. With `accept_external_processing: true`, it records
    `external_processing_accepted_at`, resumes updates, and queues a `user` run.
  - **Lost dominates**: a lost repository never becomes paused.

### Implementation for User Story 3

- [X] T038 [US3] Add check-run semantics to backend/src/codeatlas/ingestion/pipeline.py (research
  R6):
  - After `_resolve`, a `check` run completes with "Already up to date", without changing the
    active pointer, in either of these cases:
    - a ready snapshot exists for the resolved commit at the current `index_version`;
    - the latest finished indexing job on this branch has `error_retryable = false` at the same
      `payload.commit_sha`.
  - Otherwise it indexes and publishes normally.
- [X] T039 [US3] Implement `schedule_checks(db, *, now) -> int` in
  backend/src/codeatlas/workspace/sync.py:
  - It uses the research R6 selection, calls `request_automatic_run(trigger="check")`, and holds
    its own advisory lock key.
  - Register it in backend/src/codeatlas/jobs/maintenance.py with
    `register_periodic("access_checks", timedelta(minutes=30), ...)`.
  - Make the scheduling parts of T036 pass.
- [X] T040 [US3] Pause automatic updates in the pipeline (research R8):
  - In backend/src/codeatlas/ingestion/pipeline.py:
    - `UserAuthorizationInvalid` calls `pause(..., "sign_in_required")` and
      `forget_github_credential(owner_id)`.
    - A resolved repository with `private=True` and no `external_processing_accepted_at` updates
      `is_private`, calls `pause(..., "external_processing_not_accepted")`, and fails the job with
      `external_processing_not_accepted` (permanent) before fetching.
  - Add `forget_github_credential(user_id)` to backend/src/codeatlas/auth/github_login.py. It
    deletes the credential row and leaves sessions intact.
  - Depends on T029 and T038, which edit the same file.
- [X] T041 [US3] Implement the authorization-revoked handler in
  backend/src/codeatlas/workspace/sync.py:
  - `AuthorizationRevoked` finds the user by `github_user_id`, deletes their credential, and
    pauses the active repositories of their owned workspace with `sign_in_required`.
  - It returns `processed`, or `ignored` when the user is unknown.
  - Register the handler.
- [X] T042 [US3] Resume at sign-in in backend/src/codeatlas/auth/github_login.py:
  - `complete_login`, after storing tokens, resumes the user's repositories paused with
    `sign_in_required` (`resume(via="sign_in", actor_user_id=user.id)`).
  - It calls `request_automatic_run(trigger="check")` for each.
  - Make the resume part of T037 pass.
- [X] T043 [US3] Accept the disclosure on re-index, and show pauses (contracts/http-api.md):
  - In backend/src/codeatlas/api/routes/repositories.py, `IndexIn` gains
    `accept_external_processing: bool = False`.
  - In backend/src/codeatlas/workspace/repositories.py, `reindex` handles a repository paused for
    disclosure:
    - without acceptance, it returns 422 `external_processing_not_accepted`;
    - with acceptance, it records it, calls `resume(via="acceptance")`, and enqueues a `user` run.
  - `RepositoryOut.automatic_updates` is `{state: "on" | "paused", reason}`.
- [X] T044 [US3] Show pauses in the frontend:
  - In frontend/src/components/AutomaticUpdatesStatus.tsx, show
    "Automatic updates paused. Sign in again to resume." with a sign-in link for
    `sign_in_required`.
  - For `external_processing_not_accepted`, explain that the repository became private.
  - In frontend/src/app/repositories/[id]/repository-detail.tsx, the re-index form shows the
    external-processing acceptance checkbox while the repository is paused for it.
  - `npm run lint`, `npm run typecheck`, and `npm run build` pass.

**Checkpoint**: All three stories work. Missed pushes and silent access loss are caught within 24
hours.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Isolation, log hygiene, end-to-end coverage, documentation, and final validation

- [X] T045 [P] Extend the isolation suite in backend/tests/integration/test_isolation.py:
  - Hubot never sees octocat's `latest_push`, `access`, or `automatic_updates` data.
  - A lost repository in octocat's workspace returns 404, not 403, to hubot on every endpoint.
  - A push delivery for a repository both users connected creates jobs that each read only their
    own workspace (FR-010).
- [X] T046 [P] Extend the log-hygiene test in backend/tests/integration/test_logging.py:
  - Process a signed push delivery whose payload contains commit messages, author emails, and file
    names, then run its job.
  - Assert that none of these, nor the webhook secret or signature, appear in the captured logs.
- [X] T047 Add Playwright tests in frontend/tests/e2e/push-and-revocation.spec.ts, against the
  stack in fake mode (research R10). Signed deliveries are sent with Node's `crypto` HMAC and
  `GITHUB_WEBHOOK_SECRET`.
  - Sign in, connect `sample-app`, and wait for `ready`.
  - Send a push delivery for its current head. The repository page shows the latest push, and a
    run labeled "After a push".
  - Send `installation.deleted` for installation 5001. The page shows "Access lost" with a purge
    date, and opening the browser shows the access-lost notice.
  - Choose Re-index. The page shows "Checking access…" with no error from job polling, then the
    repository returns to "Ready".
  - Make frontend/tests/e2e/global-setup.ts fail early when `GITHUB_WEBHOOK_SECRET` is unset.
- [X] T048 Wire the secret into the `e2e` job of .github/workflows/ci.yml:
  - Pass the generated `GITHUB_WEBHOOK_SECRET` from T002 into the Compose stack's environment
    file and the Playwright process.
  - Run the new spec together with the 001 spec.
- [X] T049 [P] Update README.md:
  - Describe automatic re-indexing and access revocation in the overview.
  - Link specs/002-push-reindexing (spec, plan, research, data model, both contracts, and
    quickstart).
  - Add ADR 0007 to the decision list.
- [X] T050 [P] Extend the superseded-in-part note at the top of docs/design/codeatlas-v1.md:
  - For `specs/002-push-reindexing`, ADR 0007 applies.
  - Webhook deliveries commit with their jobs, without an outbox.
  - Installation status lives on repository rows.
- [X] T051 Run final validation:
  - Every automated check in specs/002-push-reindexing/quickstart.md.
  - Validation scenarios 1 to 12 with the real GitHub App (needs T003).
  - The SC-001 query: at least 95% of push delays under 5 minutes.
  - The webhook response time over the pushes in scenario 12, from the API access log: p95 under
    1 second.
  - Scan the changes for absolute local paths, secrets, personal notes, and references to
    private material before committing.
  - Fix any failure until every test passes.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies. T003 is a developer action needed only by T051.
- **Foundational (Phase 2)**: depends on T001. It blocks every story.
- **User Story 1 (Phase 3)**: depends on Foundational.
- **User Story 2 (Phase 4)**: depends on Foundational. Its tests start runs through manual
  re-index, so they do not need User Story 1, except the push-to-lost-repository cases in T027,
  which need T022. T029 and T030 edit pipeline.py after T023, so do them after US1 when both are
  planned.
- **User Story 3 (Phase 5)**: depends on User Story 1 (`request_automatic_run`, T021) and User
  Story 2 (loss marking and restoration in the pipeline, T029).
- **Polish (Phase 6)**: depends on every story. T047 needs T026, T035, and T044.

### Shared files (do these tasks sequentially)

- backend/src/codeatlas/ingestion/pipeline.py: T023, then T029, T030, T038, T040.
- backend/src/codeatlas/workspace/sync.py: T015, then T022, T031, T039, T041.
- backend/src/codeatlas/jobs/maintenance.py: T024, then T034, T039.
- backend/src/codeatlas/api/routes/repositories.py: T025, then T033, T043.
- backend/tests/integration/test_maintenance.py: T020, then T028.
- frontend/src/app/repositories/[id]/repository-detail.tsx: T026, then T035, T044.

### Within Each User Story

- Tests are written first and fail before implementation.
- Queue and domain functions come before handlers and pipeline changes, which come before API
  fields and frontend work.
- A story is complete when its tests and the checks in quickstart.md pass.

### Parallel Opportunities

- **Setup**: T002 alongside T001.
- **Foundational tests**: T004 to T010 are all [P], in different files.
- **Foundational implementation**: T014 alongside T011, T012, and T013. T015 needs T011 and T014;
  T016 needs T011.
- **US1 tests**: T017 to T020 in parallel.
- **US2 tests**: T027 and T028 in parallel. T031 (sync.py) and T032 (routes) can run alongside
  T029 and T030 (pipeline.py).
- **US3 tests**: T036 and T037 in parallel.
- **Polish**: T045, T046, T049, and T050 in parallel.

---

## Parallel Example: Foundational tests

```bash
Task: "Unit tests for signature checks in backend/tests/unit/test_webhook_signature.py"
Task: "Unit tests for payload parsing in backend/tests/unit/test_webhook_events.py"
Task: "Unit tests for GitHub error classification in backend/tests/unit/test_github_client_errors.py"
Task: "Unit tests for the new fake-gateway switches in backend/tests/unit/test_fake_github.py"
Task: "Integration tests for the endpoint skeleton in backend/tests/integration/test_webhooks.py"
Task: "Integration tests for access-state transitions in backend/tests/integration/test_access_state.py"
```

## Parallel Example: User Story 1

```bash
Task: "Integration tests for coalescing in backend/tests/integration/test_coalescing.py"
Task: "Integration tests for pushes in backend/tests/integration/test_push_reindexing.py"
Task: "Fault-injection ordering tests in backend/tests/integration/test_run_ordering.py"
Task: "Retention tests in backend/tests/integration/test_maintenance.py"
```

## Parallel Example: User Story 2

```bash
# After T029 and T030 (pipeline.py), these touch separate files:
Task: "Implement installation handlers in backend/src/codeatlas/workspace/sync.py"
Task: "Deny reads of lost repositories in backend/src/codeatlas/workspace/repositories.py and the routes"
```

---

## Implementation Strategy

### MVP First (User Stories 1 and 2, both P1)

1. Complete Phase 1 (Setup) and Phase 2 (Foundational).
2. Complete User Story 1. Validate: a fake push is indexed and published with trigger `push`.
3. Complete User Story 2. Validate: an installation notification denies every content endpoint,
   and a re-index restores access.
4. **Stop and validate** quickstart scenarios 1 to 8 with the real App. This is the smallest
   release that is safe to run, because automatic fetching stops when access is lost.

User Story 1 alone is demonstrable, but it keeps 001's behavior on access loss: the run fails,
and the data stays readable. Do not run it against real private repositories without User Story
2.

### Incremental Delivery

1. Foundation, then User Story 1: automatic updates, demonstrable in fake mode.
2. Add User Story 2: revocation. Release candidate.
3. Add User Story 3: daily check and pauses, which close the remaining gaps.
4. Polish: isolation, log hygiene, end-to-end tests, documentation, and final validation.

---

## Notes

- [P] tasks touch different files and have no dependency on an incomplete task.
- The [Story] label maps each task to its user story for traceability.
- Commit after each task or logical group, in Conventional Commits format with the task ID, for
  example `feat(webhooks): verify GitHub signatures (T014)`.
- tasks.md stays untracked, as in 001.
- No task writes to GitHub. T003 is configured by the developer in GitHub's settings.
