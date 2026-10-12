# HTTP API Contract Changes: Pilot Deployment

This document lists only changes to the HTTP API contracts of
[001](../../001-repository-qa/contracts/http-api.md),
[002](../../002-push-reindexing/contracts/http-api.md), and
[003](../../003-pr-review/contracts/http-api.md). Conventions, pagination, idempotency, and the
error shape are unchanged. Data shapes refer to [data-model.md](../data-model.md).

## Public routing

In every hosted environment, the web app is the only service with a public address: the domain
the hosting platform gives it, served over HTTPS, with HTTP redirected to HTTPS by the platform's
edge. The web app forwards the API's paths to the API over the platform's private network
(research R3); in development, it forwards the same paths to `localhost:8000`.

| Path | Served by |
| --- | --- |
| `/v1/*`, `/auth/*`, `/webhooks/*`, `/status/*`, `/readyz`, `/version` | API, forwarded by the web app |
| Everything else | Web app |

`/healthz` stays on the API only; the platform's restart policy and the readiness check use the
private address. Every web app response carries `Strict-Transport-Security: max-age=31536000`.

## New errors

| Status | Code | When |
| --- | --- | --- |
| 429 | `pilot_limit_reached` | The pilot-wide daily limit for questions or reviews is used up (FR-005). Message: "CodeAtlas has reached today's limit of <limit> <questions or reviews> for all pilot users." `details`: `resets_at`, `limit`, and `allowance` (`questions` or `reviews`), as for `daily_limit_reached` |
| 429 | `rate_limited` | More than `RATE_LIMIT_PER_MINUTE` requests to `/auth/*`, `/webhooks/*`, or `/status/*` from one client address in the last minute (FR-006). Sent with a `Retry-After` header in seconds, before any other processing |

`POST /v1/analysis-runs` can return `pilot_limit_reached` for both kinds. The pilot-wide check runs
before the workspace check, so a request refused by both reports `pilot_limit_reached`.

## Client address

The rate limit counts the client address that the platform's edge reports, not the address of the
web app that forwards the request (FR-006, research R4). The API takes it from the forwarded
headers only when the request comes from the private network; a request that reached the API
another way (development) uses the connection's address.

## `GET /readyz` (not in the OpenAPI document)

Readiness (FR-011), and the platform's deployment check for the API (FR-022). Runs `SELECT 1` with
a 2-second timeout. It does not contact GitHub or the model providers.

- 200 `{"status": "ready"}`
- 503 with the standard error body:
  `{"error": {"code": "not_ready", "message": "The database is unavailable.", "retryable": true, ...}}`

`GET /healthz` is unchanged: liveness, always 200 while the process runs.

## `GET /version` (not in the OpenAPI document)

```json
{ "commit": "3f9c2e1d4b5a69788c7d0e1f2a3b4c5d6e7f8091" }
```

`commit` is `CODEATLAS_RELEASE`, or else the platform's `RAILWAY_GIT_COMMIT_SHA`, or
`"development"` when neither is set (FR-019). No authentication; it reveals only the public commit.

## `GET /status/{condition}` (new; not in the OpenAPI document)

One endpoint per alert condition of FR-014 that the platform cannot see:
`/status/worker`, `/status/jobs-waiting`, `/status/jobs-failing`, and `/status/backup`. The
external monitor checks each every 3 minutes and alerts on 503, with a recovery notice on the next
200 (research R10). The conditions and thresholds are in the data model, "Alert conditions".

- 200 `{"condition": "<condition>", "status": "ok", "value": <number or null>, "threshold": <number>}`
- 503 `{"condition": "<condition>", "status": "failing", "value": <number or null>, "threshold": <number>}`
- 503 with the standard `not_ready` error body when the database is unreachable, so the monitor
  also alerts then.
- 404 `not_found` for any other condition name.

| Condition | `value` | `threshold` |
| --- | --- | --- |
| `worker` | Seconds since the newest heartbeat, or null when there is none | `WORKER_SILENT_SECONDS` |
| `jobs-waiting` | Seconds since the oldest runnable job became due, 0 when none | `JOBS_WAITING_ALERT_SECONDS` |
| `jobs-failing` | Counted failures in the last hour | `JOBS_FAILING_ALERT_COUNT` |
| `backup` | Hours since the newest completed backup, or null when there is none | `BACKUP_MISSING_HOURS` |

No authentication: the bodies hold only these numbers, and the paths are rate-limited per client
address like `/auth/*`. Responses carry `Cache-Control: no-store`.

## `GET /auth/github/callback` (changed)

When the access list applies (data model, "Access decision") and the GitHub user is not on it:

- 302 to `/?error=not_invited`;
- no session cookie, user, workspace, or stored token; and
- one audit event, `sign_in` with outcome `denied`.

Other outcomes are unchanged.

## Rate-limited paths

`GET /auth/github/login`, `GET /auth/github/callback`, `POST /auth/logout`,
`POST /webhooks/github`, and every `GET /status/{condition}` count toward the per-address limit. A
refused webhook delivery is not recorded in `webhook_deliveries`; GitHub reports it as failed in
the App's delivery log, and the daily check catches any missed push (002).

## Web routes

- `/` with `error=not_invited`: explains that the pilot is by invitation, with no sign-in button.
- Every page's footer shows the short commit of the running release.
- The question and review forms show `pilot_limit_reached` as the server's message followed by
  "New questions can be asked after <local time>." or "New reviews can be requested after <local
  time>."
- The external processing disclosure adds: "Deleted data can remain in encrypted backups for up to
  10 days after deletion."
