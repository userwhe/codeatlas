# ADR 0007: GitHub App webhooks for change notifications

- **Status**: Accepted
- **Date**: 2026-10-05
- **Scope**: `specs/002-push-reindexing` and later features that react to GitHub events

## Context

Connected repositories must follow pushes to their default branch. CodeAtlas must also stop
serving a repository when the CodeAtlas GitHub App is uninstalled or suspended, or the repository
is removed from the installation. The App is read-only (ADR 0005), and CodeAtlas makes no write
to GitHub.

## Options considered

1. Activate the App's webhook. Receive `push` and the installation events GitHub sends to every
   App, at one public endpoint whose requests are verified by an HMAC signature.
2. Poll each repository's default branch and access on a fixed interval.
3. Create a webhook on each repository through the API.

## Decision

Option 1, with the daily check from option 2 as a backstop:

- **Endpoint**: `POST /webhooks/github` on the API. It sits outside `/v1` and has no session or
  Origin check.
- **Verification**: `X-Hub-Signature-256` is checked against `GITHUB_WEBHOOK_SECRET`, which
  production configuration requires.
- **Deduplication**: verified deliveries are stored, keyed by `X-GitHub-Delivery`, so a
  redelivery is recognized.
- **Processing**: synchronous and database-only. The delivery row, queued jobs, and access-state
  changes commit together, and the response is sent well within GitHub's 10-second limit.
- **Subscription**: `push`. `installation`, `installation_repositories`, and
  `github_app_authorization` events arrive by default. Permissions stay Contents and Metadata
  read-only.
- **Backstop**: GitHub does not retry failed deliveries automatically. A daily check of every
  connected repository catches missed pushes, and access changes that produce no event.

## Trade-off

- **Gained**:
  - Pushes and uninstalls take effect within seconds.
  - No per-repository setup, and no write permission.
  - One endpoint for every installation.
- **Accepted**:
  - A public, unauthenticated endpoint. Each forged request costs one signature check and one
    audit row. Since `specs/004-pilot-deployment`, the API limits each client address to 60
    requests a minute on this endpoint, refused before the signature check.
  - Local development needs a forwarding service, such as Smee, to receive deliveries.
  - Restoration events are ignored; the next check restores access.
- **Revisit when**: a feature needs events beyond these (for example, pull requests or CI), or
  delivery volume makes synchronous processing too slow.

Details: `specs/002-push-reindexing/research.md` R1 to R3 and R6.
