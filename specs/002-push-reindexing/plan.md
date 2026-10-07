# Implementation Plan: Automatic Re-indexing and Access Revocation

**Branch**: `002-push-reindexing` | **Date**: 2026-10-05 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/002-push-reindexing/spec.md`

## Summary

Connected repositories now follow pushes to their default branch without user action. CodeAtlas
also stops serving a repository as soon as it learns that the workspace owner can no longer read
it on GitHub.

Technical approach:

- **Change notifications**: the existing GitHub App's webhook is activated and subscribed to
  `push` (ADR 0007). `POST /webhooks/github` verifies each delivery's HMAC signature, deduplicates
  it by delivery ID, and applies it in one database transaction, with no GitHub calls.
- **Runs**:
  - Pushes become indexing jobs on the 001 PostgreSQL queue, with a new `trigger`
    (`user`, `push`, `check`).
  - Automatic requests coalesce, so each repository has at most one running and one waiting run.
  - 001 already serializes indexing per workspace, so runs publish in the order they started.
- **Access**:
  - Every run verifies the workspace owner's access before fetching.
  - Only definitive GitHub answers count as loss. Outages and App-credential errors never do.
  - A worker task enqueues a daily `check` run for each repository. The check catches missed pushes
    and silent access loss.
- **Revocation**:
  - Loss is recorded as a repository `access_state`. Every content read then returns 403 at once.
  - Waiting work is canceled, and running work cannot publish.
  - A later successful check restores the repository within 7 days. After 7 days, maintenance
    disconnects and purges it.
- **Pauses**: a lapsed GitHub authorization, or a repository that became private without
  acceptance, pauses automatic updates. They resume when the owner signs in again or accepts the
  disclosure.
- **Retention**: superseded versions are capped at 5 per repository.

## Technical Context

**Language/Version**: unchanged from 001. Python 3.13 (API and worker); TypeScript 5.9 on Node.js
24 LTS (frontend).

**Primary Dependencies**: unchanged from 001; no new dependency. HMAC verification uses the Python
standard library (`hmac`, `hashlib`).

**Storage**: PostgreSQL 17, as in 001. One migration adds columns to `repositories` and `jobs`,
creates `webhook_deliveries`, and narrows the dedupe index to waiting jobs.

**Testing**: pytest unit and integration tests against a real PostgreSQL, with the fake GitHub
gateway extended with new switches. Playwright end-to-end tests in fake mode send signed
deliveries. One SQL query measures push latency against real GitHub (SC-001).

**Target Platform**: unchanged. Linux containers with Docker Compose; current desktop browsers. In
real mode, local development needs a webhook forwarding service.

**Project Type**: Web application: frontend, HTTP API, and background worker.

**Performance Goals**:

- 95% of default-branch pushes become the default version within 5 minutes of delivery, for
  repositories up to 100,000 lines (SC-001).
- Webhook responses take well under GitHub's 10-second limit; the target is p95 under 1 s.
- 001's view and search targets still hold. Access-lost checks add no GitHub call to reads.

**Constraints**:

- GitHub access stays read-only (FR-022).
- Payloads are at most 25 MiB.
- No GitHub calls happen in the webhook request path.
- Access loss is detected within 1 minute of an installation notification (SC-005), and within 24
  hours otherwise (SC-006).
- The grace period is 7 days.
- At most 5 superseded versions are kept per repository.

**Scale/Scope**:

- The 001 pilot: up to 10 concurrent users and 10 repositories per workspace.
- A daily check costs 4 GitHub calls per repository.
- About 2 new UI areas on the repository page (access and automatic updates), plus a trigger label
  on versions.

## Project Rules Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Rule | Gate for this plan | Before research | After design |
| --- | --- | --- | --- |
| English only | All artifacts, ADRs, code, and commit messages in English | Pass | Pass: every artifact is in English |
| Simplicity | Every new table, column, endpoint, and task serves a current requirement; no speculative infrastructure | Pass, with the design document's outbox and installation table flagged for review | Pass: no new service or dependency. The outbox, the installation table, the generation counter, incremental snapshots, and `repository` events are deferred, each with a revisit condition (research deviations table, R1). Daily checks reuse the indexing job rather than adding a job kind (R6) |
| Tested core | Core logic has automated tests; a story is done only when its tests pass | Pass | Pass: R11 maps unit, integration, fault-injection, and end-to-end tests to every functional requirement and to SC-002 through SC-011 |
| Recorded decisions | Costly-to-reverse choices have ADRs | Pending | Pass: ADR 0007 records the webhook decision, and ADR 0005 is amended for the active webhook |
| Human in the loop | No external write without a draft and confirmation | Pass | Pass: the App stays read-only. Notifications are inbound only. The redelivery API and repository-webhook creation were rejected partly because they write to GitHub. The developer configures the App's webhook themselves |
| Repository hygiene | No absolute local paths, secrets, or personal notes committed | Pass | Pass: `GITHUB_WEBHOOK_SECRET` lives only in `.env` (gitignored), and `.env.example` gets the empty key. Fake-mode and CI secrets come from the environment |

Dependency-to-need map: no dependency is added. The new code uses these existing ones:

| Dependency | New use |
| --- | --- |
| FastAPI | `POST /webhooks/github` (FR-001, FR-002) |
| SQLAlchemy, Alembic | Migration `0002_push_reindexing`; access-state transitions and coalescing queries |
| Standard library `hmac`, `hashlib` | Signature verification (FR-002) |
| pytest, Playwright | Tests for every new behavior |

## Project Structure

### Documentation (this feature)

```text
specs/002-push-reindexing/
├── plan.md                 # This file
├── research.md             # Phase 0: decisions, rationale, alternatives
├── data-model.md           # Phase 1: schema changes and access-state machine
├── quickstart.md           # Phase 1: setup changes and validation scenarios
├── contracts/
│   ├── github-webhooks.md  # Phase 1: inbound webhook contract
│   └── http-api.md         # Phase 1: changes to the 001 HTTP API
├── checklists/
│   └── requirements.md     # Spec quality checklist
└── tasks.md                # Phase 2 (/speckit-tasks; not created here)

docs/decisions/
├── 0005-github-app-for-identity-and-access.md   # amended: webhook active
└── 0007-github-webhooks-for-change-notifications.md
```

### Source Code (repository root)

New and changed files. Everything else is unchanged from 001.

```text
backend/
├── alembic/versions/
│   └── 0002_push_reindexing.py      # new: columns, webhook_deliveries, narrowed dedupe index
├── src/codeatlas/
│   ├── config.py                    # GITHUB_WEBHOOK_SECRET; required in production
│   ├── models.py                    # new columns and WebhookDelivery; new audit actions
│   ├── api/
│   │   ├── app.py                   # webhook router; Origin check skips /webhooks/*
│   │   └── routes/
│   │       ├── webhooks.py          # new: POST /webhooks/github
│   │       ├── repositories.py      # access, automatic_updates, latest_push, trigger; acceptance on re-index
│   │       ├── snapshots.py, search.py, analysis_runs.py, jobs.py   # 403 repository_access_lost
│   ├── github/
│   │   ├── gateway.py               # UserAuthorizationInvalid, AppCredentialsRejected
│   │   ├── client.py                # 401 classification by credential type
│   │   ├── fake.py                  # push, suspend, remove_from_installation, revoke_authorization, make_private, set_unavailable
│   │   └── webhooks.py              # new: signature verification and payload parsing (pure)
│   ├── workspace/
│   │   ├── access.py                # new: mark_lost, restore, pause, resume, ensure_readable
│   │   ├── sync.py                  # new: apply events, request automatic runs, schedule_checks
│   │   └── repositories.py          # readability in get_scoped*; re-index acceptance; derived state
│   ├── auth/github_login.py         # resume paused repositories at sign-in
│   ├── ingestion/pipeline.py        # owner token, loss/pause classification, restoration, check runs, guarded publish
│   └── jobs/
│       ├── queue.py                 # trigger, waiting-only coalescing for automatic requests
│       └── maintenance.py           # grace expiry, version cap, delivery retention, check scheduler registration
└── tests/
    ├── unit/                        # test_webhook_signature.py, test_webhook_events.py, test_github_client_errors.py, test_coalescing.py
    └── integration/                 # test_webhooks.py, test_push_reindexing.py, test_access_revocation.py,
                                     # test_daily_check.py, test_pauses.py, test_retention_cap.py

frontend/
├── src/
│   ├── components/
│   │   ├── RepositoryStateBadge.tsx # access_lost state
│   │   ├── RepositoryAccessPanel.tsx    # new: verified or lost, reason, purge date, guidance
│   │   ├── AutomaticUpdatesStatus.tsx   # new: on or paused, latest push and its run
│   │   └── SnapshotSelector.tsx     # trigger label per version
│   ├── app/repositories/[id]/repository-detail.tsx   # new panels; acceptance checkbox when paused
│   └── lib/api/                     # regenerated schema types; repository_access_lost handling
└── tests/e2e/
    └── push-and-revocation.spec.ts  # new: signed deliveries in fake mode

.env.example                         # GITHUB_WEBHOOK_SECRET=
.github/workflows/ci.yml             # webhook secret for the test and e2e jobs, from a generated value
README.md                            # documentation links for spec 002 and ADR 0007
```

**Structure Decision**: The same web application layout as 001 (ADR 0001).

- `github/webhooks.py` holds the pure parts: signature checks and payload parsing, so unit tests
  cover them without a database.
- `workspace/sync.py` and `workspace/access.py` hold the domain transitions as plain functions
  that take a SQLAlchemy session, following 001's style.
- No new protocols or service layers. The only external boundary is still `GitHubGateway`.

## Complexity Tracking

No rule violations. Every design-document mechanism left out is a simplification, recorded with
its revisit condition in research.md.
