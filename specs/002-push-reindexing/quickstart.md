# Quickstart and Validation: Automatic Re-indexing and Access Revocation

This guide extends [001's quickstart](../001-repository-qa/quickstart.md). Set up and run the stack
as described there first. The commands refer to files that the implementation creates; see the
source layout in [plan.md](plan.md). Run every path from the repository root.

## GitHub App changes (real mode)

In the development GitHub App's settings, make these changes:

- **Webhook**: active.
  - **URL**: a forwarding URL that reaches `http://localhost:8000/webhooks/github` (see below).
  - **Secret**: a random string with high entropy, for example from `openssl rand -hex 32`.
- **Subscribe to events**: `Push`. Installation and authorization events arrive without a
  subscription.
- **Repository permissions**: unchanged, Contents `Read-only` and Metadata `Read-only`.

Add the secret to `.env`, which is gitignored:

- `GITHUB_WEBHOOK_SECRET`: the same value as in the App settings.

GitHub cannot reach `localhost`, so forward deliveries while you test. For example, create a Smee
channel at <https://smee.io> and run:

```bash
npx smee-client --url https://smee.io/<channel> --target http://localhost:8000/webhooks/github
```

Anyone who knows a Smee channel URL can read its payloads, so use one only with test
repositories. ngrok or cloudflared work too.

In the App's **Advanced** tab, **Recent Deliveries** shows each delivery's response. It can also
redeliver one, with the same delivery ID.

## Fake mode

Use the settings from 001 (`CODEATLAS_ENV=development`, `CODEATLAS_FAKE_EXTERNALS=1`), plus a
`GITHUB_WEBHOOK_SECRET` of your choice. Tests and the end-to-end suite sign their own deliveries
with that value.

## Automated checks

Same commands as 001:

```bash
cd backend && uv run ruff check . && uv run mypy src && uv run pytest
cd frontend && npm run lint && npm run typecheck && npm run build
cd frontend && npm run test:e2e        # stack running in fake mode, with GITHUB_WEBHOOK_SECRET set for both
```

## Forcing a daily check

The worker's check scheduler runs when the worker starts and every 30 minutes after that. To make
a repository due right away:

```bash
docker compose exec db psql -U codeatlas -c \
  "UPDATE repositories SET access_checked_at = now() - interval '21 hours' WHERE full_name = '<owner/name>';"
```

Then restart the worker.

## Validation scenarios

"Fixture repository" means a public test repository that you own, connected in CodeAtlas.

| # | Scenario | Steps | Expected outcome | Covers |
| --- | --- | --- | --- | --- |
| 1 | Push re-indexes | Push a commit that adds a function to the fixture repository's default branch | Within minutes, the default version shows the new commit with "created by a push", and a symbol search finds the function | US1-1, FR-001, FR-009 |
| 2 | Other refs ignored | Push to another branch, then push a tag | Recent Deliveries shows 202 `ignored` for both; no run starts | US1-2 |
| 3 | Burst of pushes | Push 5 commits as separate pushes within one minute | No more than one run waits at any time, and the last commit becomes the default version. Pushes that arrive while a run is in progress share one follow-up run; pushes that arrive after a run has finished start their own, so a fast repository may get one run per push | US1-3, US1-4, SC-002 |
| 4 | Duplicate delivery | Redeliver a push from Recent Deliveries | 202 `duplicate`; no new run | US1-5, FR-005 |
| 5 | Forged delivery | `curl -X POST localhost:8000/webhooks/github -H 'X-GitHub-Event: push' -H 'X-GitHub-Delivery: test' -H 'X-Hub-Signature-256: sha256=00' -d '{}'` | 401 `invalid_signature`; a `webhook_rejected` audit event; nothing else changes | US1-8, FR-002, SC-004 |
| 6 | Pinned answers survive pushes | Ask a question, push a commit, then reopen the answer | The answer still shows the earlier commit and its content | US1-7 |
| 7 | Repository removed from the installation | In the App installation's settings on GitHub, remove the fixture repository | Within a minute, the repository shows "Access lost" with the reason and purge date; browse, search, versions, and answers return 403 `repository_access_lost` | US2-2, FR-014, SC-005 |
| 8 | Access restored | Add the repository back to the installation, then choose Re-index | The repository is readable again with its earlier versions and answers; the run reports that the commit is already indexed | US2-5, FR-015, SC-011 |
| 9 | Missed push caught up | Stop the forwarder, push a commit, restart the forwarder, then force a daily check | The commit is indexed, with "created by the daily check" | US3-2, FR-018, SC-009 |
| 10 | Up-to-date check | Force a daily check with no new commits | The run finishes with "Already up to date"; only the last-verified time changes | US3-3 |
| 11 | Authorization revoked | On GitHub, revoke your authorization of the App (Settings, Applications, Authorized GitHub Apps) | Repositories show "Automatic updates paused: sign in again"; signing in resumes them and runs a check | US3-4, FR-016 |
| 12 | Push latency | Push 20 commits, about one every 2 minutes, then run the query below | At least 95% of the delays are under 5 minutes | SC-001 |

These behaviors run against the fake gateway in the integration tests, because real GitHub cannot
easily produce them on demand:

- a GitHub outage during checks (SC-008);
- out-of-order completion (SC-003);
- loss detected by a run (US2-1) and by the daily check (US3-1);
- the 7-day grace expiry (SC-011);
- the version cap (SC-010);
- the private-repository pause (FR-017).

### Push latency query (SC-001)

```sql
SELECT d.detail->>'after' AS pushed_sha,
       d.received_at,
       covering.finished_at - d.received_at AS delay
FROM webhook_deliveries d
JOIN repositories r
  ON r.github_repository_id = d.github_repository_id AND r.deleted_at IS NULL
CROSS JOIN LATERAL (
  SELECT j.finished_at FROM jobs j
  WHERE j.repository_id = r.id AND j.kind = 'index_repository' AND j.status = 'succeeded'
    AND j.finished_at >= d.received_at
    AND (j.started_at >= d.received_at OR j.payload->>'commit_sha' = d.detail->>'after')
  ORDER BY j.finished_at LIMIT 1
) AS covering
WHERE d.event = 'push' AND d.outcome = 'processed'
ORDER BY d.received_at;
```

Each row pairs a push with the first successful run that covers it: a run that started after the
push was received, and therefore resolved a head at least as new, or the run that indexed exactly
the pushed commit. A run's success means its version was published.

The query uses runs rather than versions, because the version cap (research R9) removes older
versions within minutes once 5 newer ones exist.
