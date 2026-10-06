# ADR 0003: PostgreSQL-backed job queue

- **Status**: Accepted
- **Date**: 2026-10-03
- **Scope**: `specs/001-repository-qa`; revisit at cloud deployment

## Context

Indexing and question answering run in the background. They must survive browser closes and
worker crashes, retry transient failures, and publish exactly one accepted result per job. Each
workspace may have at most one job of each kind running at a time. The design document proposed
SQS with a transactional outbox, a relay, a reconciler, and a dead-letter queue. Job state would
stay in PostgreSQL either way.

## Options considered

1. The PostgreSQL `jobs` table as the queue: workers claim with `FOR UPDATE SKIP LOCKED`, with
   leases and fencing tokens.
2. SQS with a transactional outbox (the design document).
3. Celery with Redis.

## Decision

Option 1:

- **Claiming**: a worker claims a job atomically, increments its attempt counter and fencing
  token, and holds a 60-second lease renewed every 20 seconds.
- **Fencing**: every publishing write checks the fencing token.
- **Recovery**: expired leases can be reclaimed.
- **Concurrency**: a partial unique index allows one running job per kind per workspace.
- **Retries**: at most 3 attempts, with capped exponential backoff and jitter.
- **Isolation of retries**: each indexing attempt builds into its own snapshot row instead of
  resuming from checkpoints.

## Trade-off

- **Gained**:
  - No broker, relay, reconciler, or cloud emulator.
  - Job state and queue state cannot disagree.
  - Workspace concurrency is enforced by the database.
- **Accepted**:
  - Workers poll every second.
  - There is no dead-letter tooling; failed jobs stay visible in the `jobs` table instead.
  - A retried indexing attempt redoes earlier stages.
- **Revisit when**:
  - a second consumer type appears;
  - operations need dead-letter handling; or
  - polling load becomes measurable.

Details: `specs/001-repository-qa/research.md` R3.
