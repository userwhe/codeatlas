# Research: Repository Q&A with Citations

Phase 0 output for [plan.md](plan.md). Each entry records the decision, the rationale, and the
alternatives considered. Decisions that are costly to reverse also have an ADR in
`docs/decisions/`. Versions are the current stable releases as of 2026-10-03; exact versions are
pinned in lockfiles at setup.

## Unknowns resolved

| Unknown in Technical Context | Resolution |
| --- | --- |
| Application languages and frameworks | R1, ADR 0001 |
| Where source contents, search data, and vectors live | R2, ADR 0002 |
| Job execution mechanism for this increment | R3, ADR 0003 |
| Progress delivery to the browser | R4 |
| Sign-in and repository access model | R5, ADR 0005 |
| Generation model and provider | R11, ADR 0006 (supersedes the generation part of ADR 0004) |
| Embedding model and provider | R12, ADR 0004 |

## Deviations from `docs/design/codeatlas-v1.md`

The design document predates the project's simplicity rule: build only what the current
requirements need. Applying that rule to this increment changes these points. Each one names the condition for revisiting it.

| Design document | This increment | Revisit when |
| --- | --- | --- |
| SQS with a transactional outbox, relay, and reconciler | PostgreSQL-backed job queue (R3) | A second consumer type, dead-letter tooling, or measured polling load |
| S3 for source bundles and artifacts | File contents stored in PostgreSQL (R2) | Database size or backup time becomes a measured problem |
| SSE event stream | Persisted events with cursor polling (R4) | Token-level answer streaming is added |
| Per-stage job checkpoints | Each attempt rebuilds into a fresh snapshot row (R3) | Indexing retries become a measured cost |
| 2,000 output tokens per call | 16,000-token output cap; answer size bounded by schema (R11) | Not applicable: the chosen model's thinking tokens are billed as output and need headroom |
| Webhooks, installation records, periodic access recheck | Deferred to the automatic re-indexing feature | That feature |

---

## R1. Application stack

**Decision**:

- Backend: Python 3.13, FastAPI 0.142, Pydantic 2.13, SQLAlchemy 2.1 (synchronous ORM with the
  psycopg 3.3 driver), Alembic 1.20, pydantic-settings for configuration. The API and the worker
  share one package, `codeatlas`, with two entry points.
- Frontend: TypeScript 5.9 on Node.js 24 LTS, Next.js 16 (App Router), React 19, Tailwind CSS 4,
  TanStack Query 5. Request and response types are generated from the backend's OpenAPI document
  with openapi-typescript 7 and called through openapi-fetch.
- Tooling: uv for Python versions and dependencies, Ruff for lint and format, mypy (strict for
  `codeatlas`), pytest; npm, ESLint, Playwright 1.63.

**Rationale**: This is the stack in the design document. Python has first-party Google GenAI
and Voyage SDKs and maintained tree-sitter bindings. FastAPI derives the OpenAPI contract from the
Pydantic models, and the frontend consumes generated types, so the contract has a single source.
Synchronous SQLAlchemy keeps transaction boundaries explicit and avoids async lazy-loading
pitfalls; at 10 concurrent users the request threadpool is ample, and slow external calls happen
in the worker. TypeScript 5.9 is used instead of 7.0 because the Next.js build-time type check
depends on the JavaScript compiler API.

**Alternatives considered**:

- Java and Spring Boot API with a Python worker: two backend languages and duplicated domain
  models for no current need.
- Full-stack TypeScript (NestJS API and worker): workable, but splits parsing and evaluation
  tooling away from the Python ecosystem the design relies on.
- Django: heavier, and OpenAPI generation is not built in.
- Async SQLAlchemy: more complexity with no measured throughput need.

## R2. Storage: PostgreSQL as the only stateful service

**Decision**: PostgreSQL 17 with the pgvector 0.8 and pg_trgm extensions holds relational
records, the job queue, file contents (`files.content`, UTF-8 text up to 1 MiB per file), lexical
search structures (`tsvector` and trigram GIN indexes), and documentation embeddings
(`vector(1024)`). Vector search is exact and filtered by snapshot; no approximate index.

**Rationale**: One stateful service locally and in CI. File contents and the rows that reference
them commit atomically, which removes the design document's upload-then-commit protocol and the
orphan-cleanup job. Volumes are small: at most 10 repositories of 100 MiB each per workspace, and
hundreds to low thousands of documentation chunks per snapshot, so an exact vector scan within one
snapshot is fast.

**Alternatives considered**:

- S3 for source contents (design document): deferred; adds a second store and a consistency
  protocol with no current need.
- OpenSearch, Elasticsearch, or Zoekt for code search: another service to operate. Trigram and
  full-text indexes are expected to meet the 1.5 s search target at this scale; the performance
  check (R14) verifies it.
- A dedicated vector database such as Milvus, Qdrant, or Pinecone: already rejected by the design
  document; pgvector suffices.

## R3. Background jobs: PostgreSQL-backed queue

**Decision**: The `jobs` table is both the state authority and the queue. A separate worker
process polls every second, and again immediately after finishing a job:

- **Claim**: select one eligible job with `FOR UPDATE SKIP LOCKED`, increment `attempt` and
  `fencing_token`, and set `lease_expires_at` to 60 seconds from now.
- **Lease renewal**: a heartbeat renews the lease every 20 seconds. If renewal finds a different
  fencing token, the worker abandons the attempt.
- **Fencing**: every publishing write checks the fencing token in the same transaction.
- **Recovery**: expired leases are claimable again, which also recovers jobs from crashed
  workers.
- **One running job per kind** (FR-027): a partial unique index on `(workspace_id, kind)` where
  `status = 'running'`.
- **Retries** (FR-030): capped exponential backoff with full jitter (base 5 s, cap 60 s), at most
  3 attempts.
- **Deadlines** (FR-031): measured from first start, 15 minutes for indexing and 3 minutes for a
  question.
- **Indexing attempts**: each attempt builds into its own new snapshot row, so a stale attempt
  never mixes rows with the current one. Only the fenced publish step marks a snapshot ready and
  moves the repository's active pointer.

**Rationale**: The requirements are durability across browser closes and worker crashes, one
accepted result per job, and per-workspace concurrency. The database that already owns job state
meets all of them. Dropping the broker removes the outbox relay, the reconciler, visibility-timeout
renewal, and a cloud emulator in local tests.

**Alternatives considered**:

- SQS with a transactional outbox (design document): deferred, as above.
- Celery with Redis: a new service, and the same fencing work is still needed for one accepted
  result.
- Temporal: a heavyweight platform for two job kinds.
- FastAPI `BackgroundTasks`: in-process and lost on restart; rejected by the design document.

## R4. Progress delivery: persisted events with cursor polling

**Decision**: Workers append `job_events` with a per-job, monotonically increasing `seq`. While a
job is active, the browser polls `GET /v1/jobs/{id}/events?after=<seq>` every 1.5 seconds using a
TanStack Query refetch interval, and stops at a terminal status.

**Rationale**: This meets FR-008 and FR-026. A client that returns resumes from its last `seq`
and receives every event it missed. It uses plain request/response, with no stream proxying,
buffering, or heartbeats. Token streaming is out of scope.

**Alternatives considered**: SSE (design document), to adopt with token streaming; WebSockets,
which add bidirectional messaging that is not needed.

## R5. GitHub identity and repository access

**Decision**: One GitHub App handles both concerns.

- **Sign-in**: the App's user authorization (OAuth web flow) issues expiring user tokens with
  refresh tokens.
- **Repository reads**: short-lived installation access tokens, minted per job and never stored.
- **App permissions**: repository `Contents: read` and `Metadata: read` only. No webhooks in this
  increment.
- **Access check** (FR-003): the user can see the repository, and an installation of the App
  that the user can access covers it.
  - `GET /repositories/{id}` with the user's token proves the user can see it. A user token can
    read every public repository, so this call alone does not prove the second condition.
  - `GET /repos/{owner}/{repo}/installation` with the App JWT finds the covering installation
    (404 when the App is not installed there).
  - That installation must appear in `GET /user/installations`. Otherwise another account's
    installation would be used for a public repository. For a private one, GitHub already
    guarantees it.
  - The check runs at connection and again at the start of each indexing job.
  - The full installation listing (`GET /user/installations/{installation_id}/repositories`)
    is used only to fill the connect dialog.
- **Fast submissions** (SC-007): connecting makes the three access-check calls and no others. The
  installation listing runs concurrently with the other two, and every call reuses connections
  from one pooled HTTP client per process. Each GitHub call takes about 200 to 400 ms, plus about
  165 ms for a new TLS connection. Three sequential calls on new connections took about 1.4 s;
  this layout measured a p95 of about 0.6 s over 20 real connections. A re-index submission
  makes no GitHub call. Branch resolution happens inside the indexing job.
- **Renames, transfers, and reinstalls**: before fetching, each indexing job refreshes `full_name`
  and `default_branch` from `GET /repositories/{id}`, and refreshes the installation ID from
  `GET /repos/{owner}/{repo}/installation`. A renamed or transferred repository, or one whose App
  was reinstalled, therefore keeps working.
- **Source fetch**: the job resolves the branch to a commit SHA. An unknown branch fails with
  `branch_not_found`; a repository with no branches fails with `repository_empty`. The job then
  streams `GET /repos/{owner}/{repo}/tarball/{sha}`. Extraction uses Python's `tarfile` and checks
  each member: absolute paths, `..` segments, links, and device files are rejected and recorded.
  Only the extraction safety caps (R7) are enforced while reading.
- **Token storage**: user tokens are encrypted at rest with Fernet (`cryptography`); the key comes
  from configuration.
- **Libraries**: httpx for HTTP; PyJWT with `cryptography` for the App JWT.

**Rationale**: One App means one consent screen and the narrowest permissions. Downloading the
tarball needs no git binary and runs no clone-time hooks (FR-011).

**Alternatives considered**:

- An OAuth App for sign-in plus a GitHub App for access: two registrations and broader OAuth
  scopes.
- Personal access tokens: users would paste secrets, with no least privilege.
- `git clone`: needs a git binary, has more attack surface, and fetches history that is not
  needed.
- githubkit or PyGithub: a handful of endpoints does not justify a client library.

## R6. Sessions and CSRF protection

**Decision**: Server-side sessions in a `sessions` table.

- **Cookie**: a 256-bit random token in `codeatlas_session` (`HttpOnly; Secure; SameSite=Lax;
  Path=/`), stored server-side as a SHA-256 hash.
- **Lifetime**: absolute expiry after 7 days; sign-out sets `revoked_at` (FR-005).
- **CSRF**: state-changing requests must carry an `Origin` header equal to the configured app
  origin, otherwise 403. This is in addition to `SameSite=Lax`.
- **Same origin**: in development, Next.js rewrites `/v1/*` and `/auth/*` to the API. A later
  deployment routes the same prefixes at the load balancer.

**Alternatives considered**:

- JWTs in browser storage: exposed to XSS and cannot be revoked server-side.
- Auth.js in Next.js: puts identity in the presentation layer, while the design document assigns
  authorization to the API.
- Double-submit CSRF tokens: more moving parts than an Origin check on a same-origin JSON API.

## R7. Source filtering and limits

**Decision**: Filters run during extraction, in this order. The first match decides the coverage
reason.

1. **Path safety**: unsafe paths and links are rejected (`unsafe_path`, `link`).
2. **Excluded directories** (`excluded_directory`): `.git`, `node_modules`, `vendor`, `.venv`,
   `venv`, `site-packages`, `__pycache__`, `dist`, `build`, `.next`, `coverage`, and similar.
   Each is recorded once as a directory entry, not once per file.
3. **Credential files** (`credential_file`): `.env*` except `.env.example`, `*.pem`, `*.key`,
   `*.p12`, `id_rsa*`, `id_ed25519*`, `credentials*.json`, and similar.
4. **Generated files** (`generated`): lockfiles, `*.min.js`, `*.map`, files marked
   `@generated` or `DO NOT EDIT` in their first 5 lines, and paths marked `linguist-generated`
   in `.gitattributes`.
5. **Binary or non-UTF-8 files** (`binary`, `unsupported_encoding`): binary means a NUL byte in
   the first 8 KiB.
6. **Size**: files over 1 MiB (`too_large`).

Limits apply at two levels:

- **Spec limits** (FR-009), counted after filtering: at most 5,000 eligible files, 100,000 source
  lines, and 100 MiB of eligible-file bytes per snapshot. Exceeding any of them fails the job with
  `limit_exceeded` and the limit's name. An eligible file is one that passes every filter, so
  excluded files never count toward these limits.
- **Extraction safety caps**, enforced while reading the archive: at most 100,000 archive members
  and 1 GiB of decompressed bytes. They guard against archive bombs and are not user-facing
  limits.

A member over 1 MiB is passed to the filters with its path and size but without its content, so
the first matching rule still decides its reason. A large file inside `node_modules/`, for
example, is recorded as `excluded_directory`, not `too_large`.

**Rationale**: Every file gets a defined, testable outcome, and nothing is executed.

**Alternatives considered**: Content secret scanners such as detect-secrets or gitleaks are a
useful extra layer later. FR-010 is satisfied by the file-name rules, so none is added now.

## R8. Code structure and lexical search

**Decision**:

- **Parser**: tree-sitter 0.25 with `tree-sitter-python` 0.25 and `tree-sitter-typescript` 0.23,
  covering both TypeScript and TSX.
- **Declarations extracted**:
  - Python: classes, functions, methods.
  - TypeScript: classes, functions, methods, interfaces, type aliases, enums, exported `const`
    functions.
- **Symbol records**: name, qualified name (for example `Repo.connect`), kind, and line range.
- **Not extracted yet**: imports, because the dependency graph is a later feature.
- **Version pin**: tree-sitter 0.26.0 returned corrupt trees and crashed on files of about
  1,000 lines, so the dependency is pinned below 0.26 and a regression test parses a large file.
- **Parse errors**: a file whose syntax tree has errors is still indexed as text and recorded as
  `unsupported_syntax` (FR-013).

Search structures:

- `files.content` and `files.path` have trigram GIN indexes, used for case-insensitive substring
  search and path search (FR-016).
- `symbols.name` has a B-tree index for exact matches and a trigram index for fuzzy matches.
  Exact matches rank first.
- `code_chunks` are 60-line windows with a 10-line overlap. Each chunk stores a `tsvector` built
  with the `simple` configuration from identifier-split text: `getUserById` becomes
  `get user by id getuserbyid`. Chunks are ranked with `ts_rank_cd` and used for question
  retrieval.

**Rationale**: tree-sitter is the parser chosen in the design document. Declaration-level symbols
cover User Story 3. Splitting identifiers lets natural-language questions match code
identifiers.

**Alternatives considered**:

- Python's `ast` module: covers Python only.
- The TypeScript compiler API: needs a Node runtime in the worker.
- Regular expressions: inaccurate, and the design document rejects them.
- Code-chunk embeddings: deferred (R12).

## R9. Documentation retrieval

**Decision**:

- **Chunking**: Markdown files (`.md`, `.mdx`) are split by heading section, at most about 1,600
  characters per chunk. Long sections split at paragraph boundaries. Each chunk keeps its heading
  path and line range.
- **Indexing**: each chunk gets an English `tsvector` and a `voyage-4` embedding
  (`input_type="document"`).
- **Query**: full-text top 20 plus exact cosine top 20 within the snapshot, combined with
  reciprocal rank fusion (k = 60). Overlapping ranges are deduplicated.
- **Query-embedding failure**: search returns full-text results with `degraded: true` (FR-017).
- **Indexing-time embedding failure**: if embedding fails after retries, the snapshot still
  becomes ready. Its documentation chunks have no vectors and coverage records
  `embeddings_unavailable`, so documentation search on that snapshot runs in reduced mode until
  the next re-index. An embedding outage therefore never blocks code browsing or Q&A.

**Alternatives considered**: Embedding-only retrieval misses exact terms. A reranker is deferred
and gated by evaluation, as in the design document.

## R10. Evidence assembly for a question

**Decision**: A bounded, deterministic pipeline with no agent loop:

1. **Pin** the snapshot. If none is specified, the repository's active snapshot is resolved once.
2. **Collect candidates**:
   - symbols and paths whose names exactly match identifier-like tokens in the question;
   - the top 20 code chunks by full-text rank;
   - the top 10 documentation chunks by hybrid rank.
3. **Fuse** the candidates with reciprocal rank fusion, symbol matches first. Expand a symbol
   match to its declaration's line range, capped at 120 lines. Merge overlapping ranges in the
   same file.
4. **Budget**: take items in rank order until about 16,000 estimated tokens (at 3.5 characters
   per token) or 12 items.
5. **Label** the evidence `E1` to `En`, each with path, commit, line range, exact excerpt, and
   SHA-256 checksum.

**Rationale**: Cost and latency are bounded and evaluation is reproducible. The design document
rules out open-ended agent loops for this release.

**Alternatives considered**: A model-driven tool loop over search tools. It is harder to bound,
and its retrieval is hard to evaluate separately from generation.

## R11. Answer generation

**Decision** (revised 2026-10-05; see ADR 0006): Google Gemini `gemini-3.8-flash`, the stable
model ID, through the Google GenAI Python SDK (`google-genai` 2.28). Calls use the Interactions
API (`client.interactions.create`), which Google recommends for new projects. The legacy
`generateContent` API remains supported and is the fallback if the Interactions API lacks a
needed option.

**Request settings**:

- `store=False`: requests are stateless. By default the Interactions API keeps every interaction,
  for 55 days on the paid tier; this application never uses `previous_interaction_id`, so nothing
  needs to be kept.
- Structured output: `response_format` with `mime_type` `application/json` and the JSON schema of
  the Pydantic `AnswerOutput` model. The response text is parsed with
  `AnswerOutput.model_validate_json`.
- `thinking_level` is set explicitly in `generation_config`, starting at `medium` (the model's
  default) and tuned on the evaluation set. The model supports `low`, `medium`, and `high`; it
  rejects `minimal`, so settings validation rejects that value too.
- Output cap of 16,000 tokens. Thinking tokens are billed as output.
- No tools, grounding, URL context, or code execution are enabled (FR-025).

**Call budget**: at most two calls per attempt, matching the design document's limit. The first
is the answer call. A repair call happens only if validation fails. It is a fresh stateless
request with the same system instruction and evidence, plus the invalid output and the
validation errors as text. No model turn is replayed, so thought signatures are not involved.

**Output schema**:

- `status`: `answered` or `insufficient_evidence`.
- `summary`.
- `claims[]`: each has `text`, `kind` (`fact` or `inference`), and `evidence_ids[]`. A fact
  needs at least one evidence ID.
- `gaps[]`.

Size bounds: at most 10 claims, at most 600 characters per claim.

**Validation**: unknown evidence IDs, facts without evidence, empty answers, and output that does
not parse against the schema are rejected. A second validation failure fails the attempt with
`citation_validation_failed`, which is not retried. Citations shown to users are built by the
server from stored evidence items, never from model text (FR-020).

**Prompt-injection defense** (FR-025):

- The system instruction states that evidence is untrusted repository content. It requires
  answering only from that evidence, and replying "insufficient evidence" otherwise.
- Evidence goes in the input, in delimited blocks with IDs.
- The model is given no tools.

**Failures**:

- 429 and 5xx responses and timeouts are retried with capped backoff inside the adapter.
- When those retries are exhausted, the job retries with backoff (FR-030).
- When the job's retries are exhausted, the run fails with `provider_unavailable`, marked
  retryable (FR-024).
- A response blocked by safety filters fails the run with `model_refused`. The exact block and
  finish-reason fields are checked against the SDK reference during implementation.

**Data handling**: Real repositories require an API key from a billing-enabled (paid-tier)
project. On the free tier, Google may use request content to improve its products, which is not
acceptable for private source excerpts (FR-006).

**Cost**: Paid-tier prices per MTok are $0.75 input and $3.75 output (thinking included) through
2026-12-31, rising to $1.50 and $7.50 from 2027-01-01. A question with about 20,000 input tokens
and 2,000 to 5,000 output tokens therefore costs about $0.02 to $0.03 now, and about $0.05 to
$0.07 from 2027. The 20-per-day limit caps spend near $1.40 per workspace per day at 2027 prices.

**Alternatives considered**:

- Claude Opus 5.5 (`claude-opus-5-5`, ADR 0004): $4 / $20 per MTok, roughly 3 to 6 times the
  cost per question. Superseded by the owner's choice. The `AnswerModel` boundary (R13) keeps a
  switch to one adapter plus configuration.
- The Gemini `generateContent` API: legacy but supported; kept as a fallback.
- Gemini through Vertex AI: the same model with Google Cloud controls. Revisit in the deployment
  feature.
- Explicit context caching: not used, because each request is single-turn with its own evidence.

## R12. Embeddings

**Decision**: Voyage AI `voyage-4` through the `voyageai` SDK (0.5), 1024 dimensions, for
documentation chunks only. The model name and dimension are part of the snapshot's
`index_version`, so vectors from different models are never mixed. Pricing is $0.06 per MTok
after 200M free tokens, so pilot usage is effectively free.

**Alternatives considered**:

- OpenAI `text-embedding-3-small`: comparable, and adds no advantage.
- Gemini embeddings: would put generation and embeddings with one vendor. Revisit if a single
  provider becomes a goal; switching requires re-indexing, because `index_version` changes.
- A local sentence-transformers model: puts a large ML runtime in the worker image.
- `voyage-code-4` embeddings for code chunks: deferred, because the design document defers code
  embeddings. Add them only if Recall@5 is below 0.80 on the evaluation set (R14).

## R13. Boundaries for external services

**Decision**: Three small `Protocol` interfaces: `GitHubGateway`, `AnswerModel`, and `Embedder`.
Each has one real implementation and one in-memory fake.

- The fakes serve fixture repositories from `backend/tests/fixtures/repos/` and return
  deterministic answers and embeddings.
- Fakes can be selected only when `CODEATLAS_ENV` is `test` or `development`. Settings validation
  rejects them in any other environment.
- No other abstraction layers: route handlers call plain domain functions that take a SQLAlchemy
  session.

**Rationale**: Core logic must have automated tests, and these three boundaries
are the only places where tests would otherwise call networked or paid services.

## R14. Testing and measurement

**Unit tests** (pytest):

- path safety, filters, and tarball extraction limits;
- symbol extraction, chunking, and identifier splitting;
- reciprocal rank fusion and the evidence budget;
- answer validation;
- job state transitions and quota arithmetic.

**Integration tests** (pytest against a real PostgreSQL from Docker Compose; a service container
in CI):

- migrations;
- two-user workspace isolation (SC-008);
- job claim, lease, and fencing, with fault injection: a worker killed mid-stage, and a stale
  attempt publishing (SC-010);
- idempotent submissions (SC-009);
- disconnect and purge (SC-011);
- API contract tests through FastAPI's `TestClient`.

**End-to-end** (Playwright, full stack with fakes): connect, index, ask, open a citation, search,
and disconnect.

**Evaluation** (manual run, real providers): under `backend/evals/`, a versioned question set
with at least 50 questions, including unanswerable ones. Each question has labeled relevant
evidence and is assigned to a tuning or held-out split, and every question runs on pinned
fixture-repository commits. The run reports Recall@5, citation validity, and abstention rate,
with denominators and failures (SC-003, SC-004, SC-006). A human audit sheet covers SC-005.

**Performance** (manual run): `backend/evals/perf_check.py` uses httpx with 10 concurrent clients
and reports p95 for searches, list views, and submissions (SC-007). Indexing time is measured on
a public repository of about 100,000 lines (SC-002).

**Pilot usability** (SC-001): 5 participants follow a scripted task.

## R15. Local environment and CI

**Decision**: Docker Compose with these services:

- `db`: the `pgvector/pgvector:pg17` image.
- `migrate`: a one-off `alembic upgrade head`.
- `api`: uvicorn.
- `worker`.
- `web`: the Next.js dev server.

GitHub Actions runs three jobs:

- backend: lint, type check, and tests, with a PostgreSQL service container;
- frontend: lint, type check, and build;
- Playwright end-to-end tests, on the main branch.

Cloud deployment is a later feature; images are built so they can be deployed unchanged.

## R16. Observability

**Decision**: Standard-library `logging` with a JSON formatter. Log lines carry `request_id`,
`job_id`, `run_id`, and `snapshot_id` where applicable. Logs never contain tokens, source text,
prompts, or model output. Audit events are database rows (FR-033). Metrics and tracing are part
of the deployment feature.

## R17. Retention and purge

**Decision**: The worker runs a maintenance pass every 10 minutes, guarded by a PostgreSQL
advisory lock. The pass removes:

- data of tombstoned repositories (FR-035, within 24 hours);
- answers older than 30 days;
- unreferenced non-active snapshots older than 14 days;
- failed or abandoned building snapshots older than 1 day;
- expired sessions;
- idempotency records older than 24 hours.
