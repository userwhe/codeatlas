# Implementation Plan: Repository Q&A with Citations

**Branch**: None created (feature directory: `specs/001-repository-qa`) | **Date**: 2026-10-03 (amended 2026-10-05: answer generation moved to Gemini 3.8 Flash; analysis findings resolved) | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/001-repository-qa/spec.md`

## Summary

This is the first CodeAtlas increment. A developer signs in with GitHub, connects a repository,
watches it index, browses and searches it at the indexed commit, and asks questions. Every answer
cites exact file and line ranges at a pinned commit, and the developer can disconnect the
repository and have its data purged.

Technical approach:

- **Applications**: a Next.js frontend; a FastAPI API; a separate Python worker sharing the API's
  package.
- **Data store**: PostgreSQL is the only stateful service. It holds records, the job queue, file
  contents, trigram and full-text search, and pgvector documentation embeddings.
- **GitHub access**: one read-only GitHub App provides sign-in and commit tarballs.
- **Indexing**: tree-sitter extracts Python and TypeScript declarations.
- **Answers**: Gemini 3.8 Flash answers from a bounded evidence set through structured JSON output
  (stateless, `store=False`).
  The server validates every cited evidence ID and builds citations from stored excerpts.

Six design-document mechanisms are simplified or deferred, because this increment builds only what
its requirements need: SQS, S3, SSE, per-stage checkpoints, the per-call output cap, and webhooks.
The reasons and revisit conditions
are in [research.md](research.md#deviations-from-docsdesigncodeatlas-v1md) and ADRs 0002 to 0004.

## Technical Context

**Language/Version**: Python 3.13 (API and worker); TypeScript 5.9 on Node.js 24 LTS (frontend)

**Primary Dependencies**:

- **Backend**: FastAPI 0.142, Pydantic 2.13, pydantic-settings, SQLAlchemy 2.1 with psycopg 3.3,
  Alembic 1.20, httpx 0.28, PyJWT 2.15 with cryptography, tree-sitter 0.25 (pinned below 0.26; Python 0.25 and
  TypeScript 0.23 grammars), pgvector 0.5 (Python), google-genai 2.28, voyageai 0.5.
- **Frontend**: Next.js 16.3, React 19.3, Tailwind CSS 4.3, TanStack Query 5, openapi-typescript
  7, openapi-fetch 0.17.

Exact versions are pinned in `backend/uv.lock` and `frontend/package-lock.json`.

**Storage**: PostgreSQL 17 with pgvector 0.8 and pg_trgm. File contents are stored in the
database, with no object store.

**Testing**:

- pytest for unit tests, and for integration tests against a real PostgreSQL. Fakes cover the
  GitHub and model-provider boundaries.
- Playwright 1.63 for end-to-end tests in fake mode.
- Scripts for the answer-quality evaluation and the performance check.

**Target Platform**: Linux containers, run with Docker Compose locally and in CI (cloud
deployment is a later feature); current desktop browsers.

**Project Type**: Web application: frontend, HTTP API, and background worker.

**Performance Goals**:

- At 10 concurrent users, p95 is under 1.5 s for searches, under 0.5 s for views of connected
  repositories, snapshots, and files, and under 1 s for submission acknowledgements (SC-007).
- Indexing a 100,000-line repository finishes within 15 minutes (SC-002).

**Constraints**:

- **Size limits** (FR-009): 5,000 eligible files, 100,000 source lines, and 100 MiB of
  eligible-file bytes per snapshot; 1 MiB per file; 10 repositories per workspace. Extraction
  safety caps: 100,000 archive members and 1 GiB decompressed (research R7).
- **Deadlines**: 15 minutes per indexing job and 3 minutes per question.
- **Model budget**: about 20,000 input tokens per question, and at most 2 model calls per attempt.
- **Usage**: 20 questions per workspace per day.
- **Access**: read-only on GitHub, and repository code is never executed.

**Scale/Scope**:

- Pilot with at most 10 concurrent users, each with a personal workspace.
- About 7 screens: sign-in, repositories, repository detail, browse, search, answer, and answer
  history.

## Project Rules Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Rule | Gate for this plan | Before research | After design |
| --- | --- | --- | --- |
| English only | All plan artifacts, ADRs, code, and commit messages in English | Pass | Pass: every artifact is in English |
| Simplicity | Each dependency and layer serves a current requirement; no speculative infrastructure | Pass, with the design-document mechanisms flagged for review | Pass: SQS, S3, SSE, checkpoints, and webhooks deferred; abstractions limited to three external-service boundaries needed for tests (R13); the dependency-to-need map is below |
| Tested core | Core logic has automated tests; a story is done only when its tests pass | Pass | Pass: R14 defines unit, integration, end-to-end, and fault-injection coverage for every core module |
| Recorded decisions | Costly-to-reverse choices have ADRs in `docs/decisions/` | Pending | Pass: ADRs 0001 to 0005 were written with this plan; ADR 0006 records the switch of answer generation to Gemini 3.8 Flash |
| Human in the loop | No external write without a draft and confirmation | Pass | Pass: the GitHub App is read-only (FR-032); model and embedding calls change no external state; Gemini requests use `store=False`; private-repository excerpts are sent only after explicit acceptance (FR-006) and only with a paid-tier key |
| Repository hygiene | No absolute local paths, secrets, or personal notes committed | Pass | Pass: `.env` gitignored, `.env.example` committed, App private key kept outside the repository |

Dependency-to-need map (every dependency serves a current need):

| Dependency | Current need |
| --- | --- |
| FastAPI, Pydantic, pydantic-settings | HTTP API, validation, OpenAPI contract, typed configuration |
| SQLAlchemy, psycopg, Alembic | Database access and explicit migrations |
| pgvector (Python) | Documentation vectors (FR-017) |
| httpx, PyJWT, cryptography | GitHub API calls, App JWT, encrypted user tokens (FR-003, FR-006) |
| tree-sitter and grammars | Symbol extraction (FR-013, FR-016) |
| google-genai | Answer generation (FR-019 to FR-024) |
| voyageai | Documentation embeddings (FR-017) |
| Next.js, React, Tailwind CSS | User interface |
| TanStack Query | Progress polling and data fetching (FR-008, FR-026) |
| openapi-typescript, openapi-fetch | Typed client generated from the API contract |
| pytest, Playwright | Automated tests for core logic; a story is done only when its tests pass |

## Project Structure

### Documentation (this feature)

```text
specs/001-repository-qa/
├── plan.md              # This file
├── research.md          # Phase 0: decisions, rationale, alternatives
├── data-model.md        # Phase 1: tables, constraints, state machines
├── quickstart.md        # Phase 1: run and validation guide
├── contracts/
│   └── http-api.md      # Phase 1: HTTP API contract
├── checklists/
│   └── requirements.md  # Spec quality checklist
└── tasks.md             # Phase 2 (/speckit-tasks; not created here)

docs/decisions/          # ADRs 0001 to 0006
```

### Source Code (repository root)

```text
backend/
├── pyproject.toml
├── uv.lock
├── alembic.ini
├── alembic/versions/
├── src/codeatlas/
│   ├── config.py               # pydantic-settings; rejects fake providers outside test/dev
│   ├── logging.py              # JSON logs with request/job/run/snapshot IDs
│   ├── db.py                   # engine, session factory
│   ├── models.py               # SQLAlchemy tables (data-model.md)
│   ├── api/
│   │   ├── app.py              # FastAPI app, error handler, request IDs, Origin check
│   │   ├── deps.py             # session -> user -> workspace resolution
│   │   └── routes/             # auth, me, github, repositories, snapshots, search, analysis_runs, jobs, usage
│   ├── auth/                   # sessions, GitHub user authorization, token encryption
│   ├── github/                 # GitHubGateway protocol, real client, fake
│   ├── ingestion/              # tarball fetch and extraction, filters, parsing, chunking, pipeline
│   ├── retrieval/              # text/path/symbol search, doc hybrid search, evidence assembly
│   ├── qa/                     # prompt, structured output schema, generation, citation validation
│   ├── providers/              # AnswerModel (Gemini) and Embedder (Voyage) protocols, real and fake
│   ├── jobs/                   # queue (claim, lease, fence), worker entry point, handlers, maintenance
│   └── workspace/              # repositories, access denial, quotas, idempotency, audit events
├── tests/
│   ├── unit/
│   ├── integration/            # real PostgreSQL; isolation, fencing, fault injection
│   └── fixtures/repos/         # sample-app, oversized, no-code, unsafe-paths
└── evals/                      # README.md, qa_v1.jsonl, run_qa_eval.py, perf_check.py

frontend/
├── package.json
├── next.config.ts              # rewrites /v1/* and /auth/* to the API in development
├── src/
│   ├── app/                    # /, /repositories, /repositories/[id], /snapshots/[id]/browse,
│   │                           # /snapshots/[id]/search, /answers/[id]
│   ├── components/             # file tree, code view with line anchors, progress, citation list
│   └── lib/api/                # generated schema types and client
└── tests/e2e/                  # Playwright, fake mode

docker-compose.yml              # db, migrate, api, worker, web
.env.example
.github/workflows/ci.yml
```

**Structure Decision**: A web application with `backend/` and `frontend/`, matching ADR 0001.
The backend is one package with two entry points: the API (`codeatlas.api.app`) and the worker
(`codeatlas.jobs.worker`). Modules are grouped by feature area. Route handlers call plain domain
functions that take a SQLAlchemy session; there are no repository or service class layers.
Protocols exist only at the three external boundaries: GitHub, the answer model, and the
embedder.

## Complexity Tracking

No rule violations. The design-document deviations above are simplifications, not added
complexity. Each is recorded in an ADR together with its revisit condition.
