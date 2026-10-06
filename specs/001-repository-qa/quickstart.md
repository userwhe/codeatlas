# Quickstart and Validation: Repository Q&A with Citations

This guide runs the feature locally and lists the scenarios that prove it works. The commands
refer to files that the implementation creates (see the source layout in [plan.md](plan.md)).
Run every path from the repository root.

## Prerequisites

| Tool | Version | Notes |
| --- | --- | --- |
| Docker with Compose v2 | Current | Docker Desktop or OrbStack; runs PostgreSQL and the full stack |
| uv | Current | Installs Python 3.13 and the backend dependencies |
| Node.js | 24 LTS | Frontend |
| GitHub App (development) | n/a | See below |
| Gemini API key (paid tier) | n/a | Answers; must come from a billing-enabled project; not needed in fake mode |
| Voyage AI API key | n/a | Documentation embeddings; not needed in fake mode |

### Register a development GitHub App

In GitHub, open Settings, then Developer settings, then GitHub Apps, then New GitHub App, and set:

- **Callback URL**: `http://localhost:3000/auth/github/callback`
- **Expire user authorization tokens**: on
- **Webhook**: inactive
- **Repository permissions**: Contents `Read-only`, Metadata `Read-only`. No other permissions.

Generate a private key, then install the App on one or more test repositories.

## Configure

```bash
cp .env.example .env
```

Fill in `.env`. It is gitignored; never commit it.

- `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET`
- `GITHUB_APP_PRIVATE_KEY_PATH`: the downloaded key, stored outside the repository
- `TOKEN_ENCRYPTION_KEY`: generate with
  `cd backend && uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `GEMINI_API_KEY`: from a billing-enabled Google AI Studio project. Free-tier keys must not be
  used with real repositories, because free-tier content may be used to improve Google's products.
- `VOYAGE_API_KEY`
- `APP_ORIGIN=http://localhost:3000`

To work offline, set `CODEATLAS_ENV=development` and `CODEATLAS_FAKE_EXTERNALS=1`. This uses the
fixture repositories and fake model providers, so no GitHub App or API keys are needed.

## Run

```bash
docker compose up -d db
docker compose run --rm migrate          # alembic upgrade head
docker compose up api worker web         # web app on http://localhost:3000
```

To run without containers for the app processes (PostgreSQL still runs in Compose):

```bash
cd backend && uv sync && uv run alembic upgrade head
uv run uvicorn codeatlas.api.app:app --reload --port 8000      # terminal 1
uv run python -m codeatlas.jobs.worker                          # terminal 2
cd frontend && npm install && npm run dev                       # terminal 3
```

## Automated checks

```bash
cd backend && uv run ruff check . && uv run mypy src && uv run pytest        # unit and integration (needs db)
cd frontend && npm run lint && npm run typecheck && npm run build
cd frontend && npm run test:e2e                                               # Playwright; needs the stack running in fake mode
```

## Validation scenarios

| # | Scenario | Steps | Expected outcome | Covers |
| --- | --- | --- | --- | --- |
| 1 | Sign in | Open `/`, choose "Sign in with GitHub", approve | You land on `/repositories`; `GET /v1/me` returns your user and workspace | FR-001, FR-002 |
| 2 | Connect and index | Choose "Connect repository", pick a test repository | Progress moves through the indexing stages, then shows the indexed commit, file counts, and skipped entries with reasons | US1-1, US1-3, FR-007, FR-008 |
| 3 | Leave during indexing | Close the tab mid-indexing and reopen the repository page | Progress resumes from the next event, or the final outcome is shown | US1-2, FR-026 |
| 4 | Private repository disclosure | Connect a private repository without accepting | Connection refused with `external_processing_not_accepted`; accepting starts indexing | US1-6, FR-006 |
| 5 | Inaccessible repository | Call `POST /v1/repositories` with a repository ID outside your installations | 404 `not_found`; no fetch appears in the worker logs | US1-5, FR-003 |
| 6 | Over-limit repository | In fake mode, connect the `oversized` fixture | Repository state `rejected` with `limit_exceeded` naming the limit | US1-4, FR-009 |
| 7 | Ask with citations | Ask "Where are repository permissions checked?" on the `sample-app` fixture | The answer lists claims with citations; each citation opens the cited lines at the indexed commit | US2-1, FR-020 |
| 8 | Insufficient evidence | Ask "Which payment provider does this use?" on `sample-app`. In fake mode, set `FAKE_ANSWER_MODEL_MODE=insufficient` first: the fake model answers whenever retrieval finds evidence | `quality_state: insufficient_evidence` with gaps listed | US2-2, FR-021 |
| 9 | Provider outage | Set `FAKE_ANSWER_MODEL_MODE=unavailable` and ask | The run fails with `provider_unavailable`, `retryable: true`; search still works | US2-3, FR-024 |
| 10 | Pinned answers | Re-index the fixture at a newer commit, then reopen the answer from scenario 7 | The answer still shows the old commit, and its citations show the old content | US2-4, FR-022 |
| 11 | Daily allowance | Set `DAILY_QUESTION_LIMIT=2` and ask three questions | The third is refused with 429 and `resets_at` | US2-6, FR-028 |
| 12 | Browse and search | Open the file tree; search symbol `check_repository_access`; search docs for "setup" | The file opens at the chosen lines; the exact symbol match is listed first; documentation passages are returned | US3-1, US3-2, US3-3 |
| 13 | Reduced documentation search | Set `FAKE_EMBEDDER_MODE=unavailable` and search docs | Keyword results with a visible reduced-mode notice | US3-4, FR-017 |
| 14 | Disconnect | Disconnect a repository with a queued job | Every page and API call for it returns 404 at once; the job is `canceled`; a maintenance pass purges its data | US4-1, US4-2, FR-034, FR-035 |

## Measurement runs (manual, real providers)

```bash
cd backend && uv run python -m evals.run_qa_eval --set evals/qa_v1.jsonl --split heldout --fork-owner <your-login>   # SC-003, SC-004, SC-006
cd backend && uv run python -m evals.perf_check --base-url http://localhost:8000 --users 10 --duration 120   # SC-007
```

- **Before the evaluation run**: fork the evaluation repositories, install the App on the forks,
  and create the pinned branches as described in `backend/evals/README.md`.
- **Answer-quality report**: shows Recall@5, citation validity, abstention rate, denominators, and
  each failure.
- **Human audit for SC-005**: uses the audit sheet that the evaluation run writes.
- **Indexing time for SC-002**: measured by connecting a public repository of about 100,000 lines
  and reading `started_at` and `finished_at` from its indexing job.
