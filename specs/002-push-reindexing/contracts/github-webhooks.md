# Inbound Contract: GitHub Webhooks

GitHub calls this endpoint; browsers never do. Decisions and alternatives are in
[research.md](../research.md) R1 and R2, and stored records are in
[data-model.md](../data-model.md).

## `POST /webhooks/github`

- **Caller**: GitHub, on behalf of the CodeAtlas GitHub App. The endpoint is served by the API
  next to `/auth/*` and `/v1/*`.
- **Authentication**: the signature only. No session cookie is read, and the Origin check is
  skipped.
- **Body**: the raw JSON payload, at most 25 MiB.

### Request headers

| Header | Use |
| --- | --- |
| `X-Hub-Signature-256` | Required. `sha256=` followed by the hex HMAC-SHA256 of the raw body, keyed by `GITHUB_WEBHOOK_SECRET` |
| `X-GitHub-Delivery` | Required. The delivery GUID, used for deduplication |
| `X-GitHub-Event` | Required. The event name |

### Processing order

1. If the body is over 25 MiB, return 413.
2. Verify the signature. If it is missing or wrong, return 401, record `webhook_rejected`, and
   stop.
3. If the delivery or event header is missing, or the body is not JSON, return 400.
4. Insert the delivery row. If the delivery ID already exists, return 202 with `duplicate`.
5. Apply the event (below), in the same transaction. Commit, then return 202.

No GitHub calls are made while handling a request.

### Responses

| Status | Body | Meaning |
| --- | --- | --- |
| 202 | `{ "outcome": "processed" }` | The event changed state or queued work |
| 202 | `{ "outcome": "ignored" }` | Valid, but not relevant (for example, a push to another branch) |
| 202 | `{ "outcome": "duplicate" }` | This delivery was already handled |
| 400 | Error shape, `invalid_delivery` | Missing headers or a body that is not JSON |
| 401 | Error shape, `invalid_signature` | The signature is missing or wrong |
| 413 | Error shape, `payload_too_large` | The body is over 25 MiB |

The error shape is the one in [001's API contract](../../001-repository-qa/contracts/http-api.md).
GitHub records any status other than 2xx as a failed delivery.

### Events

**`push`**. Fields read:

- `ref`, `after`, `deleted`;
- `repository.id`, `repository.default_branch`, `repository.private`.

Processing:

1. If `deleted` is true, or `ref` is not `refs/heads/<repository.default_branch>`, the outcome is
   `ignored`.
2. If no connected repository has `repository.id`, the outcome is `ignored`, and nothing is
   fetched.
3. Otherwise, for each connected repository with that ID, in any workspace:
   1. Set `latest_push_sha` and `latest_push_at`, and refresh `default_branch`.
   2. If the repository is `paused`, stop there.
   3. Otherwise, request a `push` run for the default branch. The coalescing rules are in
      research R3. A lost repository also gets a run; its access check may restore it.
   4. Set `latest_push_job_id` to the job that covers the push.

**`installation`**:

- `deleted`: every connected repository with `github_installation_id = installation.id` loses
  access with reason `app_uninstalled`.
- `suspend`: the same, with reason `app_suspended`.
- `created`, `unsuspend`, and `new_permissions_accepted`: `ignored`. Restoration happens through
  checks (research R7).

Losing access means: waiting jobs are canceled, reads are denied at once, and
`repository_access_lost` is recorded (research R7).

**`installation_repositories`**:

- `removed`: every connected repository with `github_installation_id = installation.id` and a
  GitHub ID in `repositories_removed[].id` loses access, with reason
  `repository_removed_from_installation`.
- `added`: `ignored`.

**`github_app_authorization`**:

- `revoked`: find the user whose `github_user_id` equals `sender.id`. Delete their stored GitHub
  credential, and pause the `active` repositories in their workspace with reason
  `sign_in_required`.

**`ping` and any other event**: `ignored`.
