# HTTP API Contract Changes: Pilot Deployment

This document lists only changes to the HTTP API contracts of
[001](../../001-repository-qa/contracts/http-api.md),
[002](../../002-push-reindexing/contracts/http-api.md), and
[003](../../003-pr-review/contracts/http-api.md). Conventions, pagination, idempotency, and the
error shape are unchanged. Data shapes refer to [data-model.md](../data-model.md).

## Public routing

In every hosted environment, Caddy serves one origin over HTTPS and redirects HTTP to HTTPS.

| Path | Served by |
| --- | --- |
| `/v1/*`, `/auth/*`, `/webhooks/*`, `/healthz`, `/readyz`, `/version` | API |
| Everything else | Web app |

## New errors

| Status | Code | When |
| --- | --- | --- |
| 429 | `pilot_limit_reached` | The pilot-wide daily limit for questions or reviews is used up (FR-005). Message: "CodeAtlas has reached today's limit of <limit> <questions or reviews> for all pilot users." `details`: `resets_at`, `limit`, and `allowance` (`questions` or `reviews`), as for `daily_limit_reached` |
| 429 | `rate_limited` | More than `RATE_LIMIT_PER_MINUTE` requests to `/auth/*` or `/webhooks/*` from one client address in the last minute (FR-006). Sent with a `Retry-After` header in seconds, before any other processing |

`POST /v1/analysis-runs` can return `pilot_limit_reached` for both kinds. The pilot-wide check runs
before the workspace check, so a request refused by both reports `pilot_limit_reached`.

## `GET /readyz` (new; not in the OpenAPI document)

Readiness (FR-011). Runs `SELECT 1` with a 2-second timeout. It does not contact GitHub or the
model providers.

- 200 `{"status": "ready"}`
- 503 with the standard error body:
  `{"error": {"code": "not_ready", "message": "The database is unavailable.", "retryable": true, ...}}`

`GET /healthz` is unchanged: liveness, always 200 while the process runs.

## `GET /version` (new; not in the OpenAPI document)

```json
{ "commit": "3f9c2e1d4b5a69788c7d0e1f2a3b4c5d6e7f8091" }
```

`commit` is the image's `CODEATLAS_RELEASE`, or `"development"` when unset (FR-019). No
authentication; it reveals only the public commit.

## `GET /auth/github/callback` (changed)

When the access list applies (data model, "Access decision") and the GitHub user is not on it:

- 302 to `/?error=not_invited`;
- no session cookie, user, workspace, or stored token; and
- one audit event, `sign_in` with outcome `denied`.

Other outcomes are unchanged.

## Rate-limited paths

`GET /auth/github/login`, `GET /auth/github/callback`, `POST /auth/logout`, and
`POST /webhooks/github` count toward the per-address limit. A refused webhook delivery is not
recorded in `webhook_deliveries`; GitHub reports it as failed in the App's delivery log, and the
daily check catches any missed push (002).

## Web routes

- `/` with `error=not_invited`: explains that the pilot is by invitation, with no sign-in button.
- Every page's footer shows the short commit of the running release.
- The question and review forms show `pilot_limit_reached` as the server's message followed by
  "New questions can be asked after <local time>." or "New reviews can be requested after <local
  time>."
- The external processing disclosure adds: "Deleted data can remain in encrypted backups for up to
  10 days after deletion."
