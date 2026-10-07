# Implementation Plan: Pull Request Review

**Branch**: `003-pr-review` | **Date**: 2026-10-06 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/003-pr-review/spec.md`

## Summary

A developer picks an open pull request in a connected repository and requests a review. The
review summarizes the change, ranks its risks, gives a checklist and test suggestions, and cites
exact lines on the correct side of the change. Every review is pinned to the base, head, and
merge-base commits it examined.

Technical approach:

- **GitHub access**: the App gains one read-only permission, `Pull requests: Read-only`
  (ADR 0008).
  - The list, submission, and freshness checks read pull requests with the owner's user token.
  - The review job uses only Contents: it compares the pinned commits to get the merge base, and
    downloads both commit archives.
- **No indexing** (ADR 0009): the job reads the head and merge-base archives with 001's extraction
  and filters, diffs changed text files with `difflib`, and stores only cited excerpts.
  - Related code and candidate tests come from a whole-word search of the head tree for changed
    declarations (001's tree-sitter parser) and module names. They are labeled as candidates.
- **Generation**: the same Gemini model and settings as 001 (ADR 0006), with a new prompt and
  schema, at most two calls, and server-side validation of every cited label.
- **Server-decided structure**: the server computes the overall risk level, risk order, summary
  areas, credential-file risks, changed and candidate tests, coverage, and the partial flag. The
  model writes only text tied to evidence labels.
- **Reuse of existing machinery**: reviews are `analysis_runs` of a new kind, on a new job kind
  that runs one at a time per workspace. They have their own daily allowance of 10, and use 001's
  idempotency, progress polling, retention, and 002's access checks.
- **Freshness**: computed when a review or the list is viewed, by comparing the current head.
  Nothing is pushed by events.
- **Copy as Markdown**: built by the server, with mentions and references neutralized. CodeAtlas
  never writes to GitHub.

## Technical Context

**Language/Version**: unchanged. Python 3.13 (API and worker); TypeScript 5.9 on Node.js 24 LTS
(frontend).

**Primary Dependencies**: unchanged; no new dependency.

- Diffs use Python's standard `difflib`.
- Archives, filters, and declaration parsing reuse 001's `ingestion` modules.
- Generation reuses `google-genai` through the existing adapter.

**Storage**: PostgreSQL 17. One migration, `0003_pull_request_review`, adds columns and check
values to `analysis_runs`, `evidence_items`, `usage_counters`, `jobs`, and `audit_events`. No table
is added.

**Testing**:

- pytest unit tests for the pure parts: diff, selection, context, validation, ordering, and
  Markdown export.
- Integration tests against PostgreSQL with the fake GitHub gateway and a fake review model,
  extended with pull request fixtures.
- Playwright end-to-end tests in fake mode.
- An evaluation runner with the real model on seeded pull requests (SC-002 to SC-007).

**Target Platform**: unchanged. Linux containers with Docker Compose; current desktop browsers.

**Project Type**: Web application: frontend, HTTP API, and background worker.

**Performance Goals**:

- 95% of reviews of pull requests up to 500 changed lines finish within 2 minutes (SC-006).
- Review pages load from the database within 001's 0.5-second view target. Freshness and the pull
  request list depend on GitHub and are excluded, as 001 excludes its GitHub repository list.
- Submissions make one GitHub call, so they stay within 001's 1-second acknowledgement target.

**Constraints**:

- GitHub access stays read-only (FR-034), and credential-file content never leaves the worker
  (FR-019).
- Per review: at most 100 files, 2,000 changed lines, 80 hunks, and 40,000 estimated tokens of
  diff text, with about 48,000 estimated input tokens in total and at most 200 evidence labels.
- At most 2 model calls per attempt and 3 attempts per review, 16,000 output tokens per call, and
  a 5-minute deadline per review.
- 10 reviews per workspace per day, and one running review per workspace.

**Scale/Scope**:

- The pilot: up to 10 concurrent users.
- About 10 reviews per workspace per day, each keeping a few hundred kilobytes for 30 days.
- 1 new page (`/reviews/[id]`) and 1 new section on the repository page.

## Project Rules Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Rule | Gate for this plan | Before research | After design |
| --- | --- | --- | --- |
| English only | All artifacts, ADRs, code, and commit messages in English | Pass | Pass: every artifact is in English, and reviews are generated in English |
| Simplicity | Every new column, endpoint, and task serves a current requirement; no speculative infrastructure | Pass, with the design document's pull request snapshots and import walk flagged for review | Pass: no new table, service, or dependency. Pull request snapshots, the import walk, webhook-driven freshness, and a stored pull request table are deferred, each with a revisit condition (research deviations table, R1, R3, R10) |
| Tested core | Core logic has automated tests; a story is done only when its tests pass | Pass | Pass: R13 maps unit, integration, and end-to-end tests to every functional requirement; R14 measures SC-002 to SC-007 |
| Recorded decisions | Costly-to-reverse choices have ADRs | Pending | Pass: ADR 0008 records the new permission and amends ADR 0005; ADR 0009 records reading archives instead of indexing, with the schema change; ADR 0006's scope now names reviews |
| Human in the loop | No external write without a draft and confirmation | Pass | Pass: the App gains only a read permission. Reviews are never posted; "Copy as Markdown" leaves posting to the user, by hand, and neutralizes mentions. The developer changes the App's permission themselves |
| Repository hygiene | No absolute local paths, secrets, or personal notes committed | Pass | Pass: no new secret. Credential files in pull request fixtures are injected by the fake gateway, as in 001, and never stored in the repository |

Dependency-to-need map: no dependency is added. The new code uses these existing ones:

| Dependency | New use |
| --- | --- |
| FastAPI, Pydantic | Pull request list, review submission, freshness, and Markdown endpoints (FR-001, FR-002, FR-015, FR-022) |
| SQLAlchemy, Alembic | Migration `0003_pull_request_review`; review runs, evidence sides, review quota |
| httpx | Four GitHub calls: list and get pull requests, compare commits, and read installation permissions |
| tree-sitter | Changed declarations on both sides of the change (FR-012) |
| google-genai | The review call through the existing adapter (FR-006 to FR-011) |
| Standard library `difflib`, `hashlib`, `re` | Diff hunks, member hashes, whole-word search |
| TanStack Query, openapi-fetch | The pull request list, review page, and freshness hooks |
| pytest, Playwright | Tests for every new behavior |

## Project Structure

### Documentation (this feature)

```text
specs/003-pr-review/
├── plan.md                 # This file
├── research.md             # Phase 0: decisions, rationale, alternatives
├── data-model.md           # Phase 1: schema changes and the review result shape
├── quickstart.md           # Phase 1: App change, evaluation, and validation scenarios
├── contracts/
│   └── http-api.md         # Phase 1: changes to the 001 and 002 HTTP API
├── checklists/
│   └── requirements.md     # Spec quality checklist
└── tasks.md                # Phase 2 (/speckit-tasks; not created here)

docs/decisions/
├── 0005-github-app-for-identity-and-access.md        # amended: Pull requests read
├── 0006-gemini-for-answer-generation.md              # amended: scope includes reviews
├── 0008-pull-request-read-access.md                  # new
└── 0009-pull-request-reviews-from-commit-archives.md # new
```

### Source Code (repository root)

New and changed files. Everything else is unchanged from 002. tasks.md names the task for each.

```text
backend/
├── alembic/versions/
│   └── 0003_pull_request_review.py  # new: run, evidence, usage, job, and audit changes
├── src/codeatlas/
│   ├── config.py                    # daily_review_limit, review_deadline, review limits, FAKE_REVIEW_MODEL_MODE
│   ├── models.py                    # new columns and check values
│   ├── api/routes/
│   │   ├── repositories.py          # GET /v1/repositories/{id}/pull-requests
│   │   ├── analysis_runs.py         # review kind on submit, list filters, review output, freshness, markdown
│   │   ├── usage.py                 # review allowance
│   │   └── jobs.py                  # review_pull_request kind
│   ├── github/
│   │   ├── gateway.py               # list_pull_requests, get_pull_request, compare_commits, get_installation_permissions
│   │   ├── client.py                # the four REST calls; fork retry; missing-commit errors
│   │   └── fake.py                  # review-app repositories, fixture pull requests, and switches
│   ├── ingestion/
│   │   ├── extract.py               # ArchiveMember.sha256 for every regular member
│   │   └── pipeline.py              # imports the access helpers moved to jobs/github_access.py
│   ├── providers/answer_model.py    # structured call generalized over the output model; fake review modes
│   ├── review/                      # new package, mirroring qa/
│   │   ├── pulls.py                 # live list with review states; permission-missing classification; freshness
│   │   ├── runs.py                  # submit (reuse, quota, audit), get_scoped, citations
│   │   ├── review.py                # job handler and stages
│   │   ├── diff.py                  # pure: changed files, renames, hunks, coverage, selection under limits
│   │   ├── context.py               # pure: changed declarations, related code, candidate and changed tests
│   │   ├── evidence.py              # pure: labels, sides, budget
│   │   ├── prompt.py                # review-v1 system instruction and escaped user content
│   │   ├── schema.py                # ReviewOutput
│   │   ├── validate.py              # pure: label, reference, and bound checks
│   │   ├── result.py                # pure: overall risk, ordering, areas, rule risks, omitted items
│   │   └── markdown.py              # pure: export with neutralized mentions and HTML
│   ├── jobs/
│   │   ├── github_access.py         # new: owner access check, GitHub error mapping, disclosure pause, publish guard
│   │   ├── queue.py                 # per-kind deadlines and timeout codes
│   │   └── worker.py                # register the review handler
│   └── workspace/
│       ├── quotas.py                # reserve_review, refund_review
│       └── audit.py                 # pull_request_review_submit
├── tests/
│   ├── fixtures/repos/review-app/   # new: fixture repository with tests
│   ├── fixtures/pull-requests/      # new: overlays and README
│   ├── unit/                        # new: test_review_diff.py, test_review_context.py, test_review_evidence.py,
│   │                                # test_review_validation.py, test_review_result.py, test_review_markdown.py,
│   │                                # test_review_prompt.py, test_gemini_review_call.py, test_archive_hashes.py,
│   │                                # test_github_pulls_client.py, test_review_eval_metrics.py;
│   │                                # extended: test_config.py, test_fake_github.py
│   └── integration/                 # new: test_review_schema.py, test_review_quota.py, test_pull_request_list.py,
│                                    # test_review_submit.py, test_review_job.py, test_review_access.py,
│                                    # test_review_freshness.py, test_review_credentials.py;
│                                    # extended: test_job_queue.py, test_isolation.py, test_logging.py
└── evals/
    ├── README.md                    # review set format
    ├── review_v1.jsonl              # new: seeded, safe, and injection items (draft for human review)
    ├── review_fixtures/             # new: overlays per item
    └── run_review_eval.py           # new: metrics for SC-002 to SC-007, audit CSV

frontend/
├── src/
│   ├── app/
│   │   ├── repositories/[id]/repository-detail.tsx   # Pull requests section
│   │   ├── answers/[id]/answer-detail.tsx            # narrows the run type to repository_qa
│   │   └── reviews/[id]/                             # new: page.tsx, review-detail.tsx
│   ├── components/
│   │   ├── PullRequestList.tsx       # new: review states and actions; permission notice
│   │   ├── RiskLevelBadge.tsx        # new: overall level and severities
│   │   ├── ReviewCitation.tsx        # new: excerpt with side, commit, and GitHub link (uses CodeView)
│   │   ├── ExternalProcessingAcceptance.tsx          # disclosure mentions pull request content
│   │   └── UsageIndicator.tsx        # review allowance
│   └── lib/api/
│       ├── reviews.ts               # new: usePullRequests, useRequestReview, useReview, useFreshness, useReviewMarkdown
│       ├── questions.ts             # question history requests kind=repository_qa
│       └── schema.d.ts              # regenerated
└── tests/e2e/
    └── pull-request-review.spec.ts  # new

README.md                            # documentation links for spec 003 and ADRs 0008 and 0009
docs/design/codeatlas-v1.md          # superseded-in-part note for spec 003
```

**Structure Decision**: The same web application layout as 001 (ADR 0001).

- `review/` mirrors `qa/`: route handlers call `runs.py`, the worker calls `review.py`, and the
  pure modules (`diff`, `context`, `evidence`, `validate`, `result`, `markdown`) take plain data,
  so unit tests cover them without a database or network.
- The only external boundaries are still `GitHubGateway` and the model adapter.
- No new protocol or service layer.

## Complexity Tracking

No rule violations. Every design-document mechanism left out is a simplification, recorded with
its revisit condition in research.md.
