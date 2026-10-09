# CodeAtlas

CodeAtlas answers questions about a GitHub repository and reviews its pull requests, citing its
evidence. Every claim in an answer points to exact file and line ranges at the indexed commit, and
a question the code does not answer gets an "insufficient evidence" result instead of a guess.

## What it does

- **Connect repositories** through a read-only GitHub App. Indexing runs in the background with
  visible progress and reports every skipped file with its reason (generated, binary, credential
  file, too large, and so on).
- **Stay current**: a push to a connected repository's default branch is indexed automatically,
  and a daily check catches pushes whose notification was missed.
- **Ask questions** about an indexed commit. Answers separate facts from inferences, and each
  citation opens the cited lines in the file browser.
- **Browse and search** the indexed commit: a file tree, file views, and text, path, symbol, and
  documentation search.
- **Review pull requests** on request: a summary of the change, risks ranked by severity with an
  overall risk level, a checklist for the human reviewer, and test suggestions. Every item cites
  the changed lines or related code on the correct side of the change, the review is pinned to
  the base, head, and merge-base commits, and it shows when new commits make it outdated. A review
  can be copied as Markdown; CodeAtlas never posts it to GitHub.
- **Stay contained**: each user has a private workspace, questions and reviews have daily
  allowances, and disconnecting a repository hides its data at once and purges it within 24 hours.
  When the owner loses access on GitHub, CodeAtlas stops serving the repository at once, and purges
  it unless access returns within 7 days. CodeAtlas never writes to GitHub and never executes
  repository code.

## Architecture

- **Frontend** (`frontend/`): Next.js, React, and TypeScript, with an API client generated from
  the API's OpenAPI document. The web app and the API share one origin.
- **API** (`backend/src/codeatlas/api`): FastAPI. It owns sign-in, authorization, workspace
  scoping, job creation, browsing, and search.
- **Worker** (`backend/src/codeatlas/jobs`): a Python process sharing the API's package. It
  downloads commit tarballs, extracts Python and TypeScript declarations with tree-sitter, builds
  the search indexes, generates answers, and reviews pull requests by diffing the merge-base and
  head archives.
- **PostgreSQL** with pgvector and pg_trgm is the only data store. It holds records, file
  contents, trigram and full-text search, documentation embeddings, and the job queue (claimed
  with `FOR UPDATE SKIP LOCKED`, with leases and fencing tokens).
- **GitHub App**: one App provides sign-in, read-only repository and pull request access, and
  webhook notifications of pushes and installation changes, verified by their HMAC signature.
- **Model providers**: Gemini 3.8 Flash generates answers and reviews in the worker from a bounded
  set of evidence as structured JSON, and the server validates every citation. Voyage AI embeds
  documentation during indexing and search queries in the API.

## Run it

[specs/001-repository-qa/quickstart.md](specs/001-repository-qa/quickstart.md) covers
prerequisites, registering a development GitHub App, configuration, and the validation scenarios.
[specs/002-push-reindexing/quickstart.md](specs/002-push-reindexing/quickstart.md) adds the
webhook setup for automatic re-indexing.

To try it without a GitHub App or API keys, use fake mode. It serves fixture repositories from
`backend/tests/fixtures/repos` and uses fake model providers.

```bash
cp .env.example .env
# In .env, set CODEATLAS_ENV=development, CODEATLAS_FAKE_EXTERNALS=1, and TOKEN_ENCRYPTION_KEY
# (generate one with:
#  cd backend && uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
docker compose up -d db
docker compose run --rm migrate
docker compose up api worker web
```

Then open http://localhost:3000. The quickstart also shows how to run the API, the worker, and the
web app outside containers.

## Checks

From the repository root:

```bash
docker compose up -d db   # integration tests create a codeatlas_test database on this server
(cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest)
(cd frontend && npm run lint && npm run typecheck && npm run build)
(cd frontend && npm run test:e2e)   # Playwright, against the stack in fake mode
```

Set `TEST_DATABASE_URL` to give a test session its own database. The performance check for the
latency targets runs against a running stack in fake mode:
`cd backend && uv run python -m evals.perf_check --users 10 --duration 120` (see the module
docstring for the required settings).

## Documentation

- Repository Q&A: [specification](specs/001-repository-qa/spec.md), with the
  [plan](specs/001-repository-qa/plan.md),
  [research notes](specs/001-repository-qa/research.md),
  [data model](specs/001-repository-qa/data-model.md), and
  [HTTP API contract](specs/001-repository-qa/contracts/http-api.md)
- Automatic re-indexing and access revocation:
  [specification](specs/002-push-reindexing/spec.md), with the
  [plan](specs/002-push-reindexing/plan.md),
  [research notes](specs/002-push-reindexing/research.md),
  [data model](specs/002-push-reindexing/data-model.md),
  [webhook contract](specs/002-push-reindexing/contracts/github-webhooks.md),
  [HTTP API changes](specs/002-push-reindexing/contracts/http-api.md), and
  [quickstart](specs/002-push-reindexing/quickstart.md)
- Pull request review: [specification](specs/003-pr-review/spec.md), with the
  [plan](specs/003-pr-review/plan.md),
  [research notes](specs/003-pr-review/research.md),
  [data model](specs/003-pr-review/data-model.md),
  [HTTP API changes](specs/003-pr-review/contracts/http-api.md), and
  [quickstart](specs/003-pr-review/quickstart.md)
- [Architecture decision records](docs/decisions/):
  [application stack](docs/decisions/0001-application-stack.md),
  [PostgreSQL as the only data store](docs/decisions/0002-postgresql-as-the-only-data-store.md),
  [PostgreSQL job queue](docs/decisions/0003-postgresql-job-queue.md),
  [model providers](docs/decisions/0004-model-providers.md),
  [GitHub App](docs/decisions/0005-github-app-for-identity-and-access.md),
  [Gemini for answers](docs/decisions/0006-gemini-for-answer-generation.md),
  [GitHub webhooks](docs/decisions/0007-github-webhooks-for-change-notifications.md),
  [read-only pull request access](docs/decisions/0008-pull-request-read-access.md), and
  [reviews from commit archives](docs/decisions/0009-pull-request-reviews-from-commit-archives.md)
