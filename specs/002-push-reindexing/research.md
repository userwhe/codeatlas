# Research: Automatic Re-indexing and Access Revocation

Phase 0 output for [plan.md](plan.md). Each entry records the decision, the rationale, and the
alternatives considered. This feature builds on `specs/001-repository-qa`; references such as
"001 R3" point to that feature's [research.md](../001-repository-qa/research.md). GitHub behavior
was checked against GitHub's webhook documentation on 2026-10-05.

## Unknowns resolved

| Unknown | Resolution |
| --- | --- |
| How CodeAtlas learns about pushes and installation changes | R1, ADR 0007 |
| How notifications are received, verified, and deduplicated | R2 |
| How pushes become runs, and how runs stay ordered | R3 |
| Which GitHub answers count as access loss | R4 |
| Whose access automatic runs verify | R5 |
| How the daily check is scheduled | R6 |
| Access states, read denial, grace period, and restoration | R7 |
| Paused automatic updates | R8 |
| Version retention under frequent pushes | R9 |
| Local development and fake mode | R10 |
| Testing and measurement | R11 |

No new dependency is needed. Signature checks use Python's standard `hmac` and `hashlib`.

## Deviations from `docs/design/codeatlas-v1.md`

The rule from 001 still applies: build only what the current requirements need.

| Design document | This increment | Revisit when |
| --- | --- | --- |
| Delivery ID, job, and outbox event committed together | The delivery row, jobs, and state changes commit in one transaction. There is no outbox, because jobs live in PostgreSQL (001 R3) | As in 001 R3 |
| An `Installation` table with permission state and revocation time | No table. The installation ID and the access state are stored on each repository row | A feature needs installation-level data that no repository carries |
| A generation counter checked at publication | Indexing is serialized per workspace, so publications happen in start order (R3) | Indexing runs in parallel within a workspace |
| Incremental snapshots that reuse unchanged content | Each run indexes the full commit | Indexing time or storage becomes a measured problem |

---

## R1. Change notifications: GitHub App webhooks

**Decision**:

- Activate the webhook of the existing CodeAtlas GitHub App and point it at
  `POST /webhooks/github` on the API.
- Subscribe to the `push` event. It needs Contents read access, which the App already has.
- `installation`, `installation_repositories`, and `github_app_authorization` events are delivered
  to every GitHub App by default, without a subscription.
- Repository permissions stay Contents `Read-only` and Metadata `Read-only`.

**Rationale**:

- One App-level endpoint receives notifications for every installed repository.
- No per-repository setup is needed, and CodeAtlas makes no write to GitHub.
- Pushes arrive within seconds, and uninstalls and suspensions arrive as they happen (SC-001,
  SC-005).

**Alternatives considered**:

- **Polling each repository's default branch every few minutes**:
  - Freshness is bounded by the interval.
  - API calls grow with repositories times frequency.
  - Uninstalls are seen only at the next poll.
  - Polling is kept only as the daily check (R6), which covers missed notifications.
- **Repository webhooks created by CodeAtlas**: these need administration write permission on
  each repository, which is an external write and breaks the read-only App.
- **Subscribing to `repository` events** (deleted, privatized, default branch changed): deferred.
  Run-start checks and the daily check cover these cases within 24 hours, and the spec does not
  require faster detection.
- **GitHub's redelivery API for missed deliveries**: requesting a redelivery is a `POST` to GitHub.
  The daily check catches missed pushes without it.

## R2. Receiving notifications

**Decision**:

- **Endpoint**: `POST /webhooks/github` on the API. It sits outside `/v1`, so it needs no session
  cookie and skips the Origin check (001 R6). The signature check replaces both.
- **Body limit**: the raw body is read up to 25 MiB, GitHub's payload cap. A larger body returns
  413.
- **Signature**:
  - `X-Hub-Signature-256` must equal `sha256=` followed by the hex HMAC-SHA256 of the raw body,
    keyed by `GITHUB_WEBHOOK_SECRET`. The comparison uses `hmac.compare_digest`.
  - The legacy SHA-1 header is ignored.
  - A missing or wrong signature returns 401 and records one `webhook_rejected` audit event, with
    no workspace and no payload content. Nothing else changes (FR-002).
- **Secret configuration**: settings validation requires `GITHUB_WEBHOOK_SECRET` in
  `production`. When it is empty in development, every delivery is rejected.
- **Deduplication**:
  - A verified delivery is inserted into `webhook_deliveries`, keyed by `X-GitHub-Delivery`, with
    `ON CONFLICT DO NOTHING`.
  - If the row already exists, the response is 202 with `duplicate`, and nothing else happens
    (FR-005). GitHub keeps the same delivery ID when a delivery is redelivered.
  - Unverified requests never create delivery rows. Otherwise a forged request could claim a
    delivery ID ahead of the real one.
- **Processing**:
  - Processing is synchronous, in the same transaction as the delivery row.
  - It touches only the database and makes no GitHub calls, so responses stay well under GitHub's
    10-second limit.
  - If the process crashes before the commit, GitHub records a failed delivery that can be
    redelivered. If it crashes after the commit, a redelivery is a duplicate.
- **Events**:

  | Event and action | Effect |
  | --- | --- |
  | `push` to the default branch | R3 |
  | `installation.deleted` | Covered repositories lose access: `app_uninstalled` (R7) |
  | `installation.suspend` | Covered repositories lose access: `app_suspended` |
  | `installation_repositories.removed` | Listed repositories lose access: `repository_removed_from_installation` |
  | `github_app_authorization.revoked` | Pause the owner's repositories: `sign_in_required` (R8) |
  | `ping`, and every other event or action | Recorded as `ignored` |

  Restoration events (`installation.created`, `installation.unsuspend`, and
  `installation_repositories.added`) are ignored. Access is restored by the next check, push, or
  re-index (R7). Matching a new installation to earlier repositories would need data that the
  repository rows do not hold.

**Accepted risk**: each unverified request writes one audit row, so a flood of forged requests
grows the audit table. Rate limiting at the edge belongs to the deployment feature.

**Alternatives considered**:

- **Queue each delivery and process it in the worker**: one more hop and one more job kind, with
  no gain, because processing is database-only and fast.
- **Store unverified deliveries in `webhook_deliveries`**: rejected because of the delivery-ID
  hijack described above.

## R3. From pushes to runs: coalescing and ordering

**Decision**:

- **Push filter**:
  - A push counts when `ref` equals `refs/heads/<repository.default_branch>`, taken from the
    payload at push time, and `deleted` is false.
  - Tag pushes, other branches, and branch deletions are recorded as `ignored`.
- **Fan-out**: every connected repository (`deleted_at IS NULL`) with that GitHub repository ID is
  handled separately, in every workspace (FR-010). For each one, the handler does the following:
  1. Record `latest_push_sha` and `latest_push_at`.
  2. Refresh `default_branch` from the payload.
  3. If automatic updates are paused, stop (R8).
  4. Otherwise, request an automatic run.
- **Automatic requests**, from a push or a check, use the 001 dedupe key
  `index:{repository_id}:{branch}`:
  1. If a waiting job (`queued` or `retry_wait`) has the key, it covers the request. A waiting
     `check` job takes the `push` trigger and the pushed SHA.
  2. Otherwise, if the running job with that key has already resolved the pushed SHA, it covers
     the request.
  3. Otherwise, a new `queued` job is created. A running job for an older commit is therefore
     followed by exactly one waiting job (FR-004).
- **Constraint change**: the partial unique index on the dedupe key now covers only waiting
  statuses (`queued` and `retry_wait`), so one running and one waiting job can share a key.
  Manual requests keep the 001 behavior, which returns any active job with the key. A manual
  request that finds a waiting `check` job gives it the `user` trigger.
- **Head resolution**: every attempt resolves the branch head when it starts (001 R5). A job that
  covers several pushes therefore indexes the newest head (FR-003).
- **Ordering** (FR-006):
  - 001 already allows one running indexing job per workspace. Every attempt resolves the head
    when it starts, and publishing is fenced.
  - Runs for a repository therefore publish in the order they started, so no generation counter
    is needed.
  - A fault-injection test checks this (SC-003).
- **Triggers**: jobs record `user`, `push`, or `check`.
  - A `user` or `push` run whose head already has a ready version reuses it and makes it active,
    which covers force pushes to an older commit.
  - A `check` run whose head is already indexed changes nothing (R6).

**Alternatives considered**:

- **A "follow-up requested" flag on the running job, acted on when it ends**: every terminal path
  (succeeded, failed, timed out, canceled) would need to honor it. A waiting job expresses the
  same thing with the existing queue.
- **A debounce window before each run**: adds latency to every push. Coalescing already folds
  every push that arrives during a run into at most one follow-up run (SC-002). Pushes spaced
  further apart than a run takes each get their own run, which is cheap: a small repository
  indexes in seconds, and the version cap (R9) bounds storage. Validation on real GitHub showed
  this: 5 pushes 1 to 2 seconds apart made 4 runs of 2 to 4 seconds each.
- **One job per pushed commit**: indexes intermediate commits that nobody asked about.

## R4. Which GitHub answers count as access loss

**Decision**: the gateway separates credential failures from access answers. It adds two
exceptions:

- `UserAuthorizationInvalid`, a subclass of `GitHubAccessDenied`, so existing callers keep
  working.
- `AppCredentialsRejected`.

Answers are classified as follows:

| GitHub answer | Classification |
| --- | --- |
| 404, or a plain 403, from `GET /repositories/{id}` with the user token | Access lost: `repository_not_visible` |
| 404 from `GET /repos/{owner}/{repo}/installation` with the App JWT | Access lost: `app_not_installed` |
| The covering installation is missing from `GET /user/installations` (suspended installations are filtered out) | Access lost: `installation_not_accessible` |
| 403 or 404 when minting the installation token, resolving the branch, or downloading the tarball | Access lost: `installation_cannot_read` |
| 401 on a user-token call, a refused token refresh, or no stored credential | Paused: `sign_in_required` (R8) |
| 401 on an App JWT call | Job failure `github_app_misconfigured`, retryable; no state change |
| 5xx, 429, a rate-limited 403, a timeout, or a connection error | Job failure `github_unavailable`, retried (001 FR-030); no state change |

A run that ends with `github_unavailable` leaves `access_checked_at` unchanged, so the next
scheduler pass (R6) tries again.

**Rationale**: only a definitive answer about the repository or installation counts (FR-013). A
misconfigured App key or a GitHub outage must never mark repositories lost (SC-008).

**Alternatives considered**: treating every `GitHubAccessDenied` as loss, as 001 does. That would
mark every repository lost when the App's private key is wrong.

## R5. Whose access automatic runs verify

**Decision**:

- Every indexing run, whatever started it, verifies the access of the workspace owner: the user
  with the `owner` membership.
- Automatic jobs have `created_by = NULL`.
- The access check is the 001 one: three calls (001 R5), then branch resolution.
- Token refresh works as in 001.
- Audit events for automatic actions have a null actor and record the trigger in `detail`.

**Rationale**: each workspace has exactly one owner, who connected its repositories. The spec
assumes automatic work runs on the owner's behalf. Using the owner for manual runs as well keeps
one code path.

## R6. The daily check

**Decision**:

- **Scheduler**: a worker periodic task, `schedule_checks`, runs every 30 minutes under its own
  advisory lock (001 R17). It enqueues a `check` job on the default branch for each repository
  that meets all of these conditions:
  - it is not tombstoned;
  - it is not paused;
  - `access_checked_at` is null or more than 20 hours old;
  - it has no waiting or running indexing job.
- **Detection bound**: a check becomes due at most 20 hours after the previous one, and the
  scheduler picks it up within 30 minutes. Including queue time, access loss and missed pushes are
  caught well within 24 hours (SC-006, SC-009).
- **`access_checked_at`**: set whenever a run reaches a definitive access result, either verified
  or lost. Transient failures leave it unchanged, so the next pass retries. Coalescing (R3) keeps
  this to one waiting job per repository.
- **A `check` run**:
  1. Verifies access, restoring the repository if it was lost (R7).
  2. Refreshes metadata.
  3. Resolves the head.
  4. Finishes with the message "Already up to date" in either of these cases:
     - a ready version exists for the head at the current index version;
     - the latest finished run on this branch failed permanently, and not retryably, at the same
       commit, for example `limit_exceeded`.
  5. Otherwise, indexes the head (FR-018).
- **Lost repositories**: checks also run for repositories in the grace period, so restoration is
  detected (US2 scenario 5).
- **Cost**: four GitHub calls per repository per day.

**Alternatives considered**:

- **A separate `check_repository` job kind**: checks could then run alongside indexing, but there
  would be a second code path for the access check, metadata refresh, and restoration.
- **Checking access on every read**: adds 0.6 s or more of GitHub latency to every view (001 R5).
- **One fixed daily time for every repository**: creates a burst of jobs. Per-repository due
  times spread the load.

## R7. Access states, read denial, the grace period, and restoration

**Decision**:

- **State**: `repositories.access_state` is `active`, `paused`, or `access_lost`, with a reason
  (data model). Access loss takes precedence over a pause.
- **Marking loss**: one transaction does all of the following:
  - locks the repository row;
  - sets the state, the reason, and `access_lost_at`; a later detection keeps the first time, so
    the grace period is not extended;
  - cancels waiting jobs;
  - records `repository_access_lost`.

  A running job for the repository cannot publish: `_publish` and `_reuse` check the state and
  discard the result, as they already do for a disconnected repository (SC-007).
- **Read denial** (FR-014):
  - These endpoints return 403 `repository_access_lost`, with `repository_id`, `reason`,
    `lost_at`, and `purge_after`, for a repository whose access is lost:
    - versions (snapshots), coverage, trees, files, and search;
    - jobs and their events;
    - analysis-run list, detail, and submission.
  - `GET /v1/repositories` and `GET /v1/repositories/{id}` still return the repository, with
    `state: access_lost`, so the owner sees what happened.
  - Other workspaces still get 404 (001 FR-004).
  - These 403 responses are not audited. The repository is the owner's own, and the browser polls,
    so audits would be noise. The `repository_access_lost` audit event records the cause once.
- **Restoration**:
  - When a run's access check passes on a lost repository, the same transaction sets `active`,
    clears the loss fields, and records `repository_access_restored`. The run then continues
    according to its trigger.
  - Runs come from the daily check, a push, or a manual re-index, which remains allowed for lost
    repositories.
- **Grace expiry**:
  - A maintenance pass tombstones every repository lost for more than 7 days. It cancels waiting
    jobs and records `repository_disconnect` with a null actor and `{"reason": "access_lost"}`.
  - The same pass purges tombstoned repositories (001 R17), so data is gone within 7 days and
    10 minutes of detection (SC-011).

**Alternatives considered**:

- **Returning 404 for lost repositories**: the owner could not tell why the repository vanished.
- **Pausing reads only for private repositories**: the spec asks for all of a lost repository's
  content to be denied.

## R8. Paused automatic updates

**Decision**:

- **`sign_in_required`**:
  - **Set**:
    - when a run gets `UserAuthorizationInvalid` (R4); the job fails with
      `github_sign_in_required`;
    - when `github_app_authorization.revoked` arrives for the owner.
  - **On set**:
    - the stored GitHub credential is deleted, because it no longer works;
    - sessions continue, because browsing needs no GitHub call;
    - `automatic_updates_paused` is recorded.
  - **Resumed**: at sign-in. `complete_login` sets the owner's paused repositories back to
    `active`, records `automatic_updates_resumed`, and enqueues a `check` job for each (FR-016).
- **`external_processing_not_accepted`**:
  - **Set**: a run finds that the repository is now private and has no acceptance on record. The
    job fails before fetching anything (FR-017). This also closes a 001 gap: a manual re-index of
    a repository that turned private.
  - **Resumed**: a re-index request with `accept_external_processing: true` records the
    acceptance, resumes updates, and queues a `user` run.
- **While paused**: pushes are recorded but start no run, and the daily check skips the
  repository.

**Accepted risk**: while updates are paused for `sign_in_required`, the owner can keep reading the
repository until their session expires (at most 7 days), with no new access check. CodeAtlas
cannot check access without the owner's GitHub authorization, and signing in again triggers a
check immediately.

**Alternatives considered**: ending the owner's sessions when their authorization lapses. This
removes the risk above, but the owner would never see the paused state that FR-016 asks the page
to show.

## R9. Version retention under frequent pushes

**Decision**:

- **Versions**: the maintenance pass (every 10 minutes) also deletes ready versions that meet all
  of these conditions (FR-019, SC-010):
  - they are not active;
  - no unexpired answer references them;
  - either 5 or more newer ready versions of the same repository exist (ranked by `ready_at` with
    a window function), or they are older than 14 days (001 R17).
- **Delivery rows**: rows older than 14 days are deleted (FR-020).
- **Jobs and job events**: still not purged. Daily checks add about 365 job rows per repository
  per year. Revisit if the table becomes a measured problem.

**Rationale**: storage stays bounded by about 6 versions per repository, plus versions pinned by
answers, however often the repository is pushed.

## R10. Local development and fake mode

**Decision**:

- **Real mode**:
  - GitHub cannot reach `localhost`. Forward deliveries with a payload delivery service, as
    GitHub's documentation suggests. For example:

    ```bash
    npx smee-client --url <channel> --target http://localhost:8000/webhooks/github
    ```

  - Anyone with a Smee channel URL can read its payloads, so use one only with test repositories.
    ngrok and cloudflared are alternatives.
  - None of these is a project dependency.
- **Fake mode and tests**:
  - Tests send signed deliveries to the endpoint, using a test secret.
  - `FakeGitHub` gains these switches:
    - `push(repository_id)` moves `main` forward and returns the push payload fields;
    - `suspend` and `remove_from_installation`;
    - `revoke_authorization(login)`;
    - `make_private`;
    - `set_unavailable`, which makes every call raise `GitHubUnavailable`.
  - A test helper builds and signs payloads.
- **End-to-end tests**:
  - Fake GitHub state lives in each process, so end-to-end tests drive only effects that the API
    applies on its own:
    - an installation-removal delivery marks the repository lost and denies browsing;
    - a push delivery records the push and starts a `push` run. In the worker's fake, `main`
      has not moved, so the run reuses the existing version.
  - Commit-level behavior is covered by integration tests, which run the worker in-process (001
    R14).

## R11. Testing and measurement

**Unit tests**:

- signature verification, including a wrong secret, a tampered body, and missing or SHA-1-only
  headers;
- event parsing and the push filter;
- the gateway's error classification (R4);
- the coalescing rules for automatic and manual requests.

**Integration tests** (real PostgreSQL, worker run in-process):

- **The endpoint** (SC-002, SC-004):
  - 100 deliveries of one push create 1 job;
  - 20 pushes arriving while a run is in progress create at most 2 runs in total, and the final
    version is the last pushed commit;
  - unverified deliveries change nothing;
  - ignored events are recorded as `ignored`.
- **Pushes** (US1):
  - a push is indexed and published with the `push` trigger;
  - a failure keeps the previous version;
  - a push to an access-lost repository is checked and restored.
- **Ordering**: fault injection with stale and reclaimed attempts (SC-003).
- **Access loss**:
  - detected by a run, by each installation notification, and by the daily check (SC-005,
    SC-006);
  - a GitHub outage marks nothing lost (SC-008);
  - no publish happens after loss (SC-007);
  - every content endpoint returns 403.
- **Grace period**: restoration within 7 days, and tombstone plus purge after 7 days (SC-011).
- **Daily check**: scheduling with an injected clock, missed-push catch-up (SC-009), and the
  "already up to date" case.
- **Pauses**: `sign_in_required` with resumption at sign-in, and `external_processing_not_accepted`
  with acceptance.
- **Version cap** (SC-010).
- **Isolation**: two workspaces connected to the same repository (FR-010).

**End-to-end tests** (Playwright, fake mode): a push delivery shows the latest push and a
push-started run; an installation-removal delivery shows "Access lost" and denies browsing.

**Measurement** (SC-001): pushes to a real test repository. The quickstart's SQL query reports
the time from delivery receipt to ready version.
