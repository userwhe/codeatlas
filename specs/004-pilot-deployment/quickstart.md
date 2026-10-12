# Quickstart and Validation: Pilot Deployment

This guide validates the pilot deployment end to end. It refers to files that the implementation
creates (see the source layout in [plan.md](plan.md)); the detailed procedures live in
`docs/operations.md`, and the interfaces in [contracts/operations.md](contracts/operations.md).
Run every path from the repository root.

Every step that writes to an external system is a developer action: `railway config apply`,
variables and secrets, environments, domains, the GitHub App, notification and usage settings,
the monitors, merges into `main` (each of which releases), and operator commands. An agent may
prepare the exact commands, plans, and summaries, and runs them only after the developer confirms
that draft; any change to it needs a new confirmation.

## Prerequisites

- A Railway account on the Hobby plan, the Railway CLI 5.42.1 or later (for `railway config` and
  `railway ssh`), signed in with `railway login`, an SSH key registered on the account, and
  Node.js 22.6 or later for `npm ci --prefix .railway`.
- The pilot GitHub App (research R5), with the pilot's callback and webhook URLs on the web app's
  Railway domain.
- Gemini (paid tier) and Voyage AI keys, as in 001.
- A Better Stack account on the free plan, for the monitors (research R10).
- For the benchmark only: a judge key (Anthropic for Claude Opus 4.5, or OpenAI for GPT-5.2) and a
  read-only GitHub token (research R16).

## Automated checks

As in 003, with the new tests, and without the infrastructure checks of the earlier design:

```bash
(cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest)
(cd frontend && npm run lint && npm run typecheck && node --test "scripts/*.test.mjs" && npm run build && npm run test:e2e)
docker build backend && docker build --target production frontend
```

The backup and restore integration tests need `pg_dump` and `pg_restore` 17 on `PATH`; CI installs
them, and locally they are skipped with a reason when missing.

## First build (summary)

The operations guide has every command. For the MVP (User Story 1), in order:

1. Create the project and apply `.railway/railway.ts`: `railway config plan`, review the diff,
   `railway config apply`. Confirm that "Wait for CI" is on for every app service before anything
   deploys.
2. Generate the web app's domain, and register the pilot GitHub App with its URLs.
3. Set the sealed secrets on `api`, `worker`, and `db`; deploy the staged changes.
4. Merge to `main` (or redeploy the current commit) and wait for `api` to pass its healthcheck,
   the other services' release gates to pass, and `web` to pass its healthcheck.
5. Add yourself and the other pilot users with `railway ssh -s api -- python -m codeatlas.ops
   pilot-users add <login>`.

User Story 2 then adds the notification rules, the usage limits, and the monitors (the backup
monitor after the first nightly backup). User Story 3 adds the deployment timings and rehearses
failed releases and rollbacks; "Wait for CI", the release gates, and the schema report are on from
the start.

## Validation scenarios

| # | Scenario | Steps | Expected outcome | Covers |
| --- | --- | --- | --- | --- |
| 1 | HTTPS | `curl -sI http://<domain>/` and open `https://<domain>/` | 301 to HTTPS; a valid certificate; `Strict-Transport-Security` present | US1-1, FR-001 |
| 2 | Pilot user flow | As a listed user: sign in, install the pilot App on a test repository, connect it, ask a question, review a pull request | Each works as in 001 and 003 with the real App and providers | US1-2 |
| 3 | Automatic re-indexing | Push to the connected repository's default branch | The new commit is indexed, marked as started by a push; the delivery reached the API through the web app | US1-4 |
| 4 | Not invited | Sign in with a second GitHub account that is not listed | The invitation page. No row for that account in `users`, `workspaces`, `github_credentials`, or `sessions`; one denied `sign_in` audit event | US1-3, FR-003, SC-004 |
| 5 | Removal | `pilot-users remove` a listed user who is signed in, then reload their page | 401 and the signed-out page; signing in again shows the invitation page | FR-004 |
| 6 | Pilot-wide limit | Set `PILOT_DAILY_QUESTION_LIMIT=2` on `api` and `worker`, deploy, and submit three questions from two workspaces; then remove the variable and deploy | The third is refused with `pilot_limit_reached` and the reset time; browsing and search still work | US1-5, FR-005 |
| 7 | Rate limit and client address | Send 70 requests to `/auth/github/login` within a minute, each with a forged `X-Real-IP: 203.0.113.9`; then one request from a second network | Requests after the 60th get 429 `rate_limited` with `Retry-After`; the API's `request` lines show the real address, not the forged one; the second network is not limited | FR-006, research R4 |
| 8 | Startup validation | In `drill`, remove `GEMINI_API_KEY` from `api` and deploy | The deployment fails; its log names `GEMINI_API_KEY` and shows no value; the previous deployment keeps serving | US1-6, FR-009 |
| 9 | Public surface | List each service's networking settings; try `psql` and `curl` against `db.railway.internal`, `api.railway.internal`, and every service's public domain from outside Railway | Only `web` has a domain; no TCP proxy exists; the private names do not resolve from outside | US1-7, FR-007, SC-005 |
| 10 | Readiness | In `drill`, remove the `db` deployment | `/readyz` returns 503 through the web app; `/healthz` on `api` returns 200 (`railway ssh -s api -- python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/healthz').status)"`); with `db` back and `GEMINI_API_KEY` invalid, `/readyz` returns 200 | FR-011 |
| 11 | Logs | Take a request ID from a failed request's response and filter `@request_id:<id>`; also search the HTTP logs for the OAuth callback | Its lines from `web`'s proxy path and the API; no token, source text, prompt, or model output in the application logs; record whether the edge's HTTP log holds the callback's `code` (research R9) | US2-5, FR-012 |
| 12 | Release | Start the probe (`while :; do for p in readyz ''; do curl -s -o /dev/null -w "/$p %{http_code}\n" "https://<domain>/$p"; done; sleep 1; done`), merge a visible change | The new commit in `/version` and the footer within 15 minutes of the merge; the longest run of failures of either path is at most 60 seconds | US3-1, SC-009 |
| 13 | Failed checks and other branches | (a) Push a commit with a failing test to a pull request branch; (b) merge a commit whose CI fails on `main` | (a) No deployment; (b) the deployments wait and are skipped; the pilot keeps serving the previous commit | US3-2, FR-019 |
| 14 | Schema report | Open a pull request that adds a migration | The `schema` job's summary and a warning annotation list the migration and the expand-then-contract rule | US3-3, FR-020 |
| 15 | Failed readiness and rollback | Change `api`'s healthcheck path to `/readyz-missing` and redeploy; restore the path; then roll back the last release in the dashboard, `api` first, then `worker`, `web`, and `backup` | The new deployment fails, the previous one keeps serving throughout, and an email arrives; each rollback serves within 5 minutes; record whether the platform ran the gates on the rollback; after a migration, the rolled-back `api` logs `migrations_skipped` and starts | US3-4, US3-6, FR-022, FR-024, SC-010 |
| 16 | Jobs during a release | Start an indexing job, then merge a change while it runs | The job finishes, resumed by the new worker if needed; one result, no duplicate | US3-1, FR-023 |
| 17 | Configuration change | Change a service's start command or a variable in `.railway/railway.ts` and run `railway config plan`; change a variable in the dashboard | The plan shows the diff with secrets redacted, and nothing changes until `apply`; the dashboard change is staged with its diff until "Deploy" | US3-7, FR-025 |
| 18 | Restarts | `railway ssh -s worker -- kill -KILL 1` during a job; `railway restart -s api` | Each service returns without manual steps; the interrupted job resumes or fails with a reason | US2-8, FR-017 |
| 19 | No stored credentials | `gh secret list`, `gh variable list`, and a search of the repository for Railway tokens | No Railway token or bucket key | US3-8, FR-025 |
| 20 | Outside installation | Install the pilot App with an account that is not on the list, and push to one of its repositories | No job and no repository row; the delivery's `webhook_deliveries` row has outcome `ignored` and only the fields in the spec's edge case; it is purged after 14 days | Spec edge case, 002 FR-010 |
| 21 | Backup | After the first night, list the bucket and the `backups` table; run `ops backup` once more by hand; restore the newest backup in `drill` | A dump and a manifest under `backups/<date>/<time>/` for each run, the earlier one untouched; one row each; the restore verifies and migrates | FR-015 |
| 22 | Whole release stops | In `drill`, set `api`'s healthcheck path to `/readyz-missing`, then merge a visible change to `main` | `api`'s new deployment fails; the gates of `web`, `worker`, and `backup` time out after 20 minutes and their deployments fail; every service keeps the previous commit in `drill`, and the pilot releases normally | FR-021, FR-022, research R7 |

## Fault tests (SC-007)

Record when each fault starts and when the email arrives; each must arrive within 10 minutes of the
condition being met (the backup alert within an hour after the 26-hour mark), and a recovery email
must follow where the spec asks for one.

| Fault | How | Expected alert |
| --- | --- | --- |
| Web app down | `railway down -s web`; afterwards `railway redeploy -s web` | Web app and API monitors fail, then recover |
| Worker down with jobs queued | `railway down -s worker`, then request a re-index | Worker monitor fails after 5 minutes without a heartbeat; recovers after the redeploy |
| Failing jobs | Set `INDEXING_DEADLINE=5` (seconds) on `worker`, deploy, re-index three repositories that take longer; then remove it | Failing jobs monitor fails on the third timeout, recovers an hour later |
| Waiting jobs | With the worker down, keep a job queued for 30 minutes (or set `JOBS_WAITING_ALERT_SECONDS=60` on `api` to prove the path quickly, then remove it) | Waiting jobs monitor fails, then recovers |
| Failed release | Scenario 15's broken healthcheck path | Railway's deployment-failed email |
| Backup skipped | Remove `backup`'s cron schedule until 26 hours after the last backup, then restore it | Backups monitor fails within an hour after the mark, recovers after the next backup |

## Recovery exercise (SC-008)

Follow "Recovery exercise" in the operations guide: duplicate `production` into `drill`, set its
secrets, remove its worker deployment, stop its web app, restore the latest backup with
`ops restore`, start the web app, add the
drill's callback URL to the pilot App, sign in on the drill's domain, and confirm your
repositories, answers, and reviews. Record the start and end times, then delete `drill` and the
callback URL. The target is at most 2 hours, losing only changes made after the backup.

## Load test (SC-011)

Duplicate `production` into `loadtest`, set its variables as research R15 lists, run the
`load-test.yml` workflow against its domain, read each service's CPU and memory from Railway's
metrics for the run, and delete `loadtest`. Write `docs/reports/load-test.md` from the workflow's
artifact and the metrics, with the steps to rerun it. The 10-user level must meet 001 SC-007's
targets; the report states the highest level that does, or that all of them did.

## Evaluation (SC-012)

```bash
cd backend
uv run python -m evals.run_bench_eval --bench-commit <pinned commit> --out evals/out/bench   # Code Review Bench, Python and TypeScript
uv run python -m evals.run_qa_eval --set evals/qa_v1.jsonl --out evals/out
uv run python -m evals.run_review_eval --set evals/review_v2.jsonl --out evals/out
```

Each runner's options and required keys are in `backend/evals/README.md`. Check the providers'
model deprecation pages first, and run the benchmark before its published judges are retired
(research R16). Write `docs/reports/evaluation.md` from their outputs, with the review status label
of each set.

## Cost and availability checks (SC-001 to SC-003)

- After 14 days: the web app monitor's availability in Better Stack over the period is at least
  99%.
- After the first full month: Railway's usage page shows at most $25 for the month.
- Daily: the provider's billing shows model spend below $15 on every day.

## Validation record

Filled in during final validation: dates, results, and any scenario not run, with the reason.

**2026-10-08, automated checks of the earlier AWS design**: backend 1,502 tests, frontend end to
end 10 tests, and the infrastructure checks passed. They are superseded by the checks above and
are kept here only as history.

**2026-10-12, automated checks** (every command in "Automated checks", run locally against
PostgreSQL 17 with pgvector in Docker):

- Backend: `ruff check`, `ruff format --check`, `mypy src` (86 files), and `pytest`: 1,606 passed,
  none skipped. The backup and restore tests ran with `pg_dump` and `pg_restore` 17 from the
  `pgvector/pgvector:pg17` image on `PATH`; without them they skip with a reason.
- Frontend: `npm run lint`, `npm run typecheck`, `node --test "scripts/*.test.mjs"` (9 passed), and
  `npm run build` pass; `npm run test:e2e` against the stack in fake mode: 10 passed. Against that
  stack, `/version`, `/readyz`, and `/status/worker` answered through the web app, and every page
  carried `Strict-Transport-Security`.
- Images: `docker build backend` (with `pg_dump` 17.11) and
  `docker build --target production frontend` succeed.
- `.railway/railway.ts` type-checks against the pinned `railway` package; `railway config plan`
  needs the developer's account and is part of T024.

**Not run yet**: every scenario on Railway (1 to 22), the fault tests, the recovery exercise, the
load test, the evaluation, the credential scan, and the cost and availability checks. They need the
Railway account, the pilot GitHub App, the Better Stack monitors, or model provider keys, and wait
for the developer actions T004, T024, T042, T045, T048, T049, T051, T054, and T056.
