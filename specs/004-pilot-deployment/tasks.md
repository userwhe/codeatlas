---

description: "Task list for Pilot Deployment"
---

# Tasks: Pilot Deployment

**Input**: Design documents from `specs/004-pilot-deployment/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/http-api.md,
contracts/operations.md, quickstart.md

**Tests**: Included. The project's tested-core rule requires automated tests for core logic, and
a story is not done until its tests pass. Within each story, write the tests first and confirm
that they fail before implementing. The host scripts' state transitions are tested with `bats`
and stub commands; the rest of the infrastructure is checked by CI (Terraform validation,
`shellcheck`, Compose and Caddy configuration checks) and by the recorded exercises in
quickstart.md.

**Organization**: Tasks are grouped by user story, so each story can be implemented and tested as
an increment.

**Developer actions**: Tasks marked "Developer action" write to an external system: the AWS
account, DNS, GitHub settings, SSM parameters, the pilot's data, or a release. An agent may prepare
the exact commands, Terraform plans, and summaries, and runs nothing until the developer confirms
that draft; any change to the draft needs a new confirmation. Reading (for example `terraform
plan`, `aws ... describe`, or Logs Insights queries) needs no confirmation.

**Test settings**: tests that need settings other than those in backend/tests/conftest.py change
the cached settings through the existing `settings` fixture (`monkeypatch.setattr(settings, ...)`),
never by changing environment variables, because `get_settings` is cached and a changed value would
leak into later tests (research R18).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: The user story the task belongs to (US1 to US4)
- Paths are relative to the repository root: `backend/src/codeatlas/`, `backend/tests/`,
  `frontend/src/`, `infra/`, `deploy/`, `.github/workflows/`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Ignore rules, new settings, and the developer's one-time accounts

- [X] T001 [P] Extend .gitignore with `.terraform/`, `*.tfstate`, `*.tfstate.*`, `*.tfplan`,
  `plan.out`, `infra/**/*.tfvars` (the committed examples end in `.tfvars.example`), and
  `infra/**/backend.hcl`. Keep `.terraform.lock.hcl` tracked, and keep every existing rule.
- [X] T002 Add the new settings to backend/src/codeatlas/config.py (data-model.md, "Settings
  added"), without validation logic yet:
  - `access_list_required: bool = False` (`ACCESS_LIST_REQUIRED`).
  - `pilot_user_limit: int = 10`.
  - `pilot_daily_question_limit: int = 30` and `pilot_daily_review_limit: int = 15` (FR-005).
  - `rate_limit_per_minute: int = 60` (FR-006; 0 disables).
  - `emit_metrics: bool = False` and `metrics_environment: str = "local"`.
  - `release: str = Field(default="development", validation_alias="CODEATLAS_RELEASE")`.
  - Keep the test suites clear of the new limits: in backend/tests/conftest.py add
    `"RATE_LIMIT_PER_MINUTE": "0"`, `"PILOT_DAILY_QUESTION_LIMIT": "1000"`, and
    `"PILOT_DAILY_REVIEW_LIMIT": "1000"`; in .env.example add the three with the same values and a
    comment that the pilot uses the defaults (60, 30, 15); in .github/workflows/ci.yml's `e2e` job,
    append the same three lines to the generated `.env`; in the docstring of
    backend/evals/perf_check.py, add them to the `export` line.
  - Add unit tests to backend/tests/unit/test_config.py for the defaults above, read with
    `Settings(_env_file=None)` and a clean environment.
- [ ] T003 Developer action (needed before T038): prepare the accounts and tools, following
  research R8 and quickstart.md "Prerequisites":
  - Install Terraform 1.11 or later, the AWS CLI v2, the Session Manager plugin, `shellcheck`, and
    `bats`.
  - In the AWS account, enable IAM Identity Center, create the developer's user with an
    administrator permission set, and run `aws configure sso` for `us-east-1`.
  - Create the Terraform state bucket `codeatlas-tfstate-<account ID>` with the AWS CLI:
    versioning on, all public access blocked, default SSE-S3 encryption.
  - Register the domain at any registrar (its name servers are changed in T038).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Schema, structured log fields, readiness and version endpoints, and production images

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

### Tests for the foundation ⚠️ (write first, confirm they fail)

- [X] T004 [P] Integration tests for the schema in backend/tests/integration/test_pilot_schema.py
  (data-model.md):
  - `pilot_users` has `github_user_id` (bigint, primary key), `github_login` (text, not null),
    `note` (text, nullable), and `added_at` (default `now()`). A `note` longer than "At most 200
    characters" is rejected by a check constraint.
  - `pilot_usage_counters` has `usage_date` (date, primary key) and `questions_count` and
    `reviews_count` (integer, "Default 0").
  - `audit_events.action` accepts `pilot_user_add`, `pilot_user_remove`, and
    `pilot_user_delete_data`, and still rejects an unknown action.
  - `alembic_version.version_num` is `0004`.
- [X] T005 [P] Extend backend/tests/unit/test_logging_redaction.py for structured fields (research
  R9):
  - `logger.info("request", extra={"fields": {...}})` puts each allow-listed key (`method`,
    `route`, `status`, `duration_ms`, `kind`, `outcome`, `attempt`, `provider`) at the top level of
    the JSON line.
  - A key outside the allow-list (for example `body` or `prompt`) is dropped.
  - Lines without `fields` are unchanged.
- [X] T006 [P] Integration tests for readiness and version in
  backend/tests/integration/test_readiness.py (contracts/http-api.md):
  - `GET /readyz` returns 200 `{"status": "ready"}` with the database up.
  - With the database session factory patched to raise `sqlalchemy.exc.OperationalError`, it
    returns 503 with the standard body `{"error": {"code": "not_ready", "retryable": true, ...}}`,
    while `GET /healthz` still returns 200.
  - With `fake_answer_model_mode` and `fake_embedder_mode` set to `unavailable` through the
    `settings` fixture, `/readyz` returns 200 (FR-011).
  - `GET /version` returns `{"commit": "development"}` by default and the value of `release` when
    set through the `settings` fixture.
  - Neither path appears in the OpenAPI document.

### Implementation for the foundation

- [X] T007 Write migration backend/alembic/versions/0004_pilot_access.py, with
  `revision = "0004"` and `down_revision = "0003"`, and update backend/src/codeatlas/models.py:
  - `PilotUser` (`pilot_users`): `github_user_id` BigInteger primary key (not autoincrement),
    `github_login` Text not null, `note` Text nullable with check `note IS NULL OR
    char_length(note) <= 200`, `added_at` server default `now()`.
  - `PilotUsageCounter` (`pilot_usage_counters`): `usage_date` Date primary key,
    `questions_count` and `reviews_count` Integer, default 0 and server default `0`.
  - Add the three actions to `AUDIT_ACTIONS`, and replace the `action` check constraint with
    `_replace_check`, as `0003_pull_request_review.py` does.
  - The downgrade deletes `audit_events` rows with the three new actions, restores the previous
    constraint, and drops both tables.
- [X] T008 Implement allow-listed structured fields in backend/src/codeatlas/logging.py:
  `JsonFormatter` reads `record.fields` (a dict passed as `extra={"fields": ...}`) and copies only
  the keys in `STRUCTURED_FIELDS = ("method", "route", "status", "duration_ms", "kind", "outcome",
  "attempt", "provider")` into the entry. Document in the module docstring that the allow-list
  keeps source text, prompts, and tokens out (research R9).
- [X] T009 Add `/readyz` and `/version` to backend/src/codeatlas/api/app.py, next to `/healthz`,
  with `include_in_schema=False`:
  - `/readyz` opens a session, runs `SET LOCAL statement_timeout = '2s'` and `SELECT 1`, and
    returns 200 `{"status": "ready"}`; on any `SQLAlchemyError` it returns `_error_response` with
    status 503, code `not_ready`, message "The database is unavailable.", and `retryable` true.
  - `/version` returns `{"commit": get_settings().release}`.
- [X] T010 [P] Release metadata and fixtures in backend/Dockerfile (research R6):
  - Add `ARG CODEATLAS_RELEASE=development` and `ENV CODEATLAS_RELEASE=$CODEATLAS_RELEASE` after
    the dependency layers, so the argument does not invalidate the dependency cache.
  - Copy `tests/fixtures/repos` and `tests/fixtures/pull-requests` to `/app/tests/fixtures/`, the
    path the fake gateway reads (`github/fake.py`, `FIXTURE_REPOS_DIR`). Do not copy the rest of
    `tests/`. `backend/.dockerignore` does not exclude them.
  - Create the user with `useradd --uid 1000 --create-home app`, so the UID that render-config
    (T029) relies on is fixed rather than a default.
- [X] T011 [P] Production web image (research R6):
  - Make frontend/Dockerfile multi-stage: `deps` (`npm ci`), `dev` (today's behavior: copy the
    source and run `npm run dev -- --hostname 0.0.0.0`), `build` (`ARG NEXT_PUBLIC_RELEASE=development`,
    `NEXT_TELEMETRY_DISABLED=1`, `npm run build`), and `production` (`node:24-slim`, copy
    `.next/standalone` and `.next/static`, run `node server.js` as the `node` user on port 3000 with
    `HOSTNAME=0.0.0.0`). The project has no `public` directory; do not copy one.
  - In frontend/next.config.ts, set `output: "standalone"`; keep the rewrites for development.
  - In docker-compose.yml, build `web` with `context: ./frontend` and `target: dev`.
  - In frontend/src/app/layout.tsx, add a footer showing "Release " followed by
    `process.env.NEXT_PUBLIC_RELEASE ?? "development"`, shortened to 7 characters only when it is
    a 40-character hexadecimal SHA.
  - Confirm `docker build --target production frontend` succeeds.

**Checkpoint**: The schema, logs, health endpoints, and images are ready; the stories can begin.

---

## Phase 3: User Story 1 - Use CodeAtlas at a public address (Priority: P1) 🎯 MVP

**Goal**: Invited users sign in at a public HTTPS address and use every existing feature;
uninvited visitors are turned away without leaving data; the pilot refuses unsafe configuration;
pilot-wide limits cap model spend; the developer builds the environment and releases by hand.

**Independent Test**: Following docs/operations.md, build the pilot, release the current `main`
commit by hand, and run quickstart scenarios 1 to 7 and 9: a listed user signs in over HTTPS,
connects a real repository, sees a push indexed, asks a question, and reviews a pull request; an
unlisted account sees the invitation page and leaves no rows.

### Tests for User Story 1 ⚠️ (write first, confirm they fail)

- [X] T012 [P] [US1] Unit tests for production settings in
  backend/tests/unit/test_production_settings.py (FR-009, research R5). Build `Settings` with
  `_env_file=None` and explicit values:
  - A complete production configuration validates. Use a temporary private key file and a
    `DATABASE_URL` other than the default.
  - For each required setting (`GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_CLIENT_ID`,
    `GITHUB_APP_CLIENT_SECRET`, `GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_WEBHOOK_SECRET`,
    `TOKEN_ENCRYPTION_KEY`, `GEMINI_API_KEY`, `VOYAGE_API_KEY`), leaving it empty raises one
    `ValidationError` whose message names that setting. Leaving three empty names all three.
  - A `GITHUB_APP_PRIVATE_KEY_PATH` that does not exist is named; an `APP_ORIGIN` starting with
    `http://` is named; the default `DATABASE_URL` is named.
  - No supplied value appears in `str(error)`: give every secret a distinctive value such as
    `secret-gemini-7f3a` and assert none occurs (this reproduces the leak in research R5).
  - `CODEATLAS_FAKE_EXTERNALS=1` with production is still rejected.
  - A development configuration with nothing set still validates.
  - `access_list_enforced` is true in production, false in development, and true in development
    with `ACCESS_LIST_REQUIRED=1`.
  - Rewrite the production case in backend/tests/unit/test_config.py (around lines 45 to 51),
    which builds a production configuration with only `GITHUB_WEBHOOK_SECRET`, to use a complete
    configuration, and keep its assertion that a missing webhook secret is refused.
- [X] T013 [P] [US1] Unit tests for the rate limiter in backend/tests/unit/test_ratelimit.py
  (FR-006, research R4):
  - `SlidingWindowLimiter(limit=3, window_seconds=60, clock=fake_clock)`: three calls for one
    address are allowed; the fourth is refused with `retry_after` equal to the seconds until the
    oldest call leaves the window, rounded up; after the window passes, calls are allowed again.
  - Two addresses are independent.
  - Idle addresses are evicted: after every address's window has passed, a sweep leaves no entry.
  - `limit=0` allows everything.
  - Through an app: set `rate_limit_per_minute` to 2 with the `settings` fixture and build a new
    app with `create_app()` (the module-level `app` was built at import with the limit off). The
    third `GET /auth/github/login` returns 429 with `{"error": {"code": "rate_limited", ...}}` and a
    `Retry-After` header; `POST /webhooks/github` is limited the same way and the refused request
    creates no `webhook_deliveries` row; `GET /v1/me`, `/healthz`, and `/readyz` are never limited.
- [X] T014 [P] [US1] Unit tests for looking up a user by login:
  - In backend/tests/unit/test_github_users_client.py, with `httpx.MockTransport`:
    `get_user_by_login("octocat")` sends `GET /users/octocat` with no `Authorization` header and
    returns a `GitHubUser`; 404 raises `GitHubNotFound`; 5xx and rate limiting raise
    `GitHubUnavailable`, as the existing calls map them.
  - Extend backend/tests/unit/test_fake_github.py: the fake returns `octocat` and `hubot`, and a
    new user `monalisa` with no installations; an unknown login raises `GitHubNotFound`; `monalisa`
    can complete the fake sign-in and list their (empty) repositories without an error; the
    existing tests for the `monalisa/public-lib` fixture still pass.
- [X] T015 [P] [US1] Integration tests for the access decision in
  backend/tests/integration/test_access_list.py (FR-002, FR-003, data-model.md "Access decision").
  Set `access_list_required` to true with the `settings` fixture:
  - With `octocat` in `pilot_users`, octocat signs in as before, and `pilot_users.github_login` is
    refreshed from GitHub.
  - `monalisa` (not listed) completing the callback gets 302 to `/?error=not_invited`, no session
    cookie, and zero rows for that GitHub user in `users`, `workspaces`, `memberships`,
    `github_credentials`, and `sessions`. Exactly one `audit_events` row has action `sign_in`,
    outcome `denied`, and detail `{"reason": "not_invited", "github_login": "monalisa"}`.
  - With the access list off (the test default), monalisa signs in normally.
- [X] T016 [P] [US1] Integration tests for the operator commands in
  backend/tests/integration/test_ops_commands.py (FR-004, contracts/operations.md), calling
  `codeatlas.ops.__main__.main([...])` and checking exit status and output:
  - `pilot-users add octocat --note friend` adds the fake's user ID, login, and note, records
    `pilot_user_add`, and exits 0. Adding it again, adding an unknown login, or adding beyond
    `pilot_user_limit` (set to 1 with the `settings` fixture) exits 1 and changes nothing.
  - `pilot-users list` prints login, user ID, note, and date added.
  - `pilot-users remove octocat` deletes the row, sets `revoked_at` on all of octocat's sessions in
    the same transaction (a signed-in client's next `GET /v1/me` returns 401), records
    `pilot_user_remove`, and exits 0; removing an unlisted login exits 1.
  - `pilot-users delete-data octocat` exits 1 while octocat is listed. After removal it tombstones
    every connected repository of octocat's workspace through the existing disconnect, deletes the
    `github_credentials` row, revokes sessions, records `pilot_user_delete_data`, and exits 0.
- [X] T017 [P] [US1] Integration tests for pilot-wide limits in
  backend/tests/integration/test_pilot_limits.py (FR-005, research R14), with
  `pilot_daily_question_limit` 2 and `pilot_daily_review_limit` 1 set through the `settings`
  fixture:
  - octocat asks one question and hubot asks one; hubot's second question gets 429
    `pilot_limit_reached` with the message "CodeAtlas has reached today's limit of 2 questions for
    all pilot users.", `details.resets_at`, `details.limit` 2, and `details.allowance`
    `questions`, and hubot's `usage_counters.questions_count` stays at 1.
  - With the pilot-wide limit high and `daily_question_limit` 1, a workspace's second question gets
    `daily_limit_reached`, and `pilot_usage_counters.questions_count` is unchanged by the refusal.
  - The review limit behaves the same with `allowance` `reviews`.
  - A review that ends `nothing_to_review` (fixture pull request #4 of `octo-org/review-app`)
    refunds both the workspace and the pilot-wide count.
  - Concurrency: with the question limit at 5, ten threads submitting different questions from
    two workspaces produce exactly 5 accepted submissions.
  - When both the workspace and the pilot-wide allowance are used up, the code is
    `pilot_limit_reached`.
  - Browsing (`GET /v1/repositories`) and search still succeed after the limit is reached.
- [X] T018 [P] [US1] Playwright tests in frontend/tests/e2e/pilot-access.spec.ts, against the
  stack in fake mode:
  - Opening `/?error=not_invited` shows the heading "CodeAtlas is in a private pilot", the
    explanation, and no "Sign in with GitHub" link.
  - After `signIn` and `connectOrOpen(page, "octo-org/sample-app")`, intercept
    `POST **/v1/analysis-runs` with `page.route` and fulfill 429 with the standard envelope
    `{"error": {"code": "pilot_limit_reached", "message": "CodeAtlas has reached today's limit of 30 questions for all pilot users.", "retryable": false, "request_id": null, "details": {"resets_at": "2030-01-01T00:00:00Z", "limit": 30, "allowance": "questions"}}}`;
    asking a question shows that message and "New questions can be asked after".
  - The footer shows "Release development".

### Implementation for User Story 1

- [X] T019 [P] [US1] Production validation in backend/src/codeatlas/config.py (FR-009, research R5):
  - Set `hide_input_in_errors=True` in `model_config`. Never log `ValidationError.errors()` or
    `.json()`, which still contain the input.
  - Replace `_webhook_secret_in_production` with one `model_validator(mode="after")` that, in
    production, collects every missing or invalid setting from T012's list (empty values, a
    private key path that is not a readable file, an `APP_ORIGIN` not starting with `https://`, a
    `DATABASE_URL` equal to the development default) and raises one `ValueError` listing their
    environment variable names, for example "Missing or invalid settings for production:
    GEMINI_API_KEY, VOYAGE_API_KEY". Never include a value. Define it after
    `_fakes_only_outside_production`, so that validator still runs first and the existing test of
    its message keeps passing.
  - Add the property `access_list_enforced` (`env == "production" or access_list_required`).
- [X] T020 [P] [US1] Rate limiting (FR-006, research R4):
  - Create backend/src/codeatlas/api/ratelimit.py with `SlidingWindowLimiter(limit,
    window_seconds=60, clock=time.monotonic)`: a dict from address to a `deque` of timestamps,
    guarded by a `threading.Lock`; `check(address) -> float | None` returns `None` when allowed or
    the retry-after seconds; a sweep every 60 seconds of calls drops addresses whose newest call
    left the window.
  - In `create_app()` in backend/src/codeatlas/api/app.py, build the limiter from
    `get_settings().rate_limit_per_minute` and register an HTTP middleware after the others, so it
    runs first. For paths starting with `/auth/` or `/webhooks/`, it checks `request.client.host`
    and returns `_error_response(request, 429, "rate_limited", "Too many requests. Try again
    later.", retryable=True)` with `Retry-After` (integer seconds) before calling the next handler.
- [X] T021 [P] [US1] Look up GitHub users by login:
  - Add `get_user_by_login(self, login: str) -> GitHubUser` to the protocol in
    backend/src/codeatlas/github/gateway.py.
  - Implement it in backend/src/codeatlas/github/client.py as an unauthenticated
    `GET /users/{login}` (public data), mapped like `get_authenticated_user`, with 404 raising
    `GitHubNotFound`.
  - In backend/src/codeatlas/github/fake.py, add `monalisa` to `_USERS` (next free fake user ID)
    and `"monalisa": set()` to `_INITIAL_ACCESS`, so calls that index `self._access` by login do
    not raise `KeyError`; implement `get_user_by_login` from `_USERS`.
- [X] T022 [US1] Create backend/src/codeatlas/auth/access_list.py (research R13):
  - `is_allowed(db, github_user_id) -> bool`.
  - `add(db, gateway, login, note) -> PilotUser`, `remove(db, login) -> PilotUser`,
    `delete_data(db, login) -> int` (repositories disconnected), and `list_users(db)`.
    `AccessListError(message)` covers each refusal in T016; none commits.
  - `add` refuses at `pilot_user_limit` rows, resolves the login with `gateway.get_user_by_login`,
    stores `github_user_id`, and records `pilot_user_add` with detail `github_user_id` and
    `github_login`.
  - `remove` deletes the row and sets `revoked_at` on the user's unrevoked sessions (match
    `users.github_user_id`, if the user exists), and records `pilot_user_remove`.
  - `delete_data` refuses while the login is listed; otherwise, for the user's owner workspace, it
    calls `workspace.repositories.disconnect` (with the loaded `User` and `Workspace` and
    `request_id=None`) for each repository not yet disconnected, calls `forget_github_credential`,
    revokes sessions, and records `pilot_user_delete_data`. The disconnect audit events keep the
    user as actor (data-model.md).
- [X] T023 [US1] Enforce the list at sign-in:
  - In backend/src/codeatlas/auth/github_login.py, right after `get_authenticated_user` in
    `complete_login`, when `get_settings().access_list_enforced` and not
    `access_list.is_allowed(db, github_user.id)`, raise `NotInvited(github_user.login)` (a new
    `SignInError` subclass) before any row is written. When allowed, update
    `pilot_users.github_login` if it changed.
  - In backend/src/codeatlas/api/routes/auth.py, catch `NotInvited` before the generic handler:
    roll back, record `sign_in` with outcome `denied` and detail `{"reason": "not_invited",
    "github_login": ...}`, commit, delete the state cookie, and redirect to `/?error=not_invited`.
- [X] T024 [US1] Operator commands in backend/src/codeatlas/ops/__init__.py and
  backend/src/codeatlas/ops/__main__.py: `python -m codeatlas.ops pilot-users add <login>
  [--note TEXT] | remove <login> | delete-data <login> | list`, with `argparse`, one session per
  command committed on success, `get_gateway()` for lookups, and the exit statuses in
  contracts/operations.md. Errors print one line to stderr. Call `configure_logging()` only under
  `if __name__ == "__main__"`, so tests that call `main([...])` keep their log capture.
- [X] T025 [US1] Pilot-wide limits in backend/src/codeatlas/workspace/quotas.py (FR-005, research
  R14):
  - Add `_reserve_pilot(db, now, *, counter, limit, allowance, noun)`: the same guarded upsert as
    `_reserve` on `PilotUsageCounter`, raising `ApiError(429, "pilot_limit_reached", f"CodeAtlas
    has reached today's limit of {limit} {noun} for all pilot users.", details={"resets_at",
    "limit", "allowance"})`.
  - `reserve_question` and `reserve_review` call `_reserve_pilot` first, then `_reserve`, in the
    caller's transaction, so a workspace refusal rolls back the pilot-wide increment.
  - `refund_review` also decrements `PilotUsageCounter.reviews_count` for the same `usage_date`,
    never below zero, and does so before the workspace decrement, so both paths lock the pilot row
    first and cannot deadlock.
- [X] T026 [P] [US1] Frontend messages:
  - In frontend/src/app/page.tsx and frontend/src/app/sign-in.tsx, when `error === "not_invited"`,
    render the heading "CodeAtlas is in a private pilot" and "Your GitHub account is not on the
    pilot's access list. CodeAtlas did not keep any data from this sign-in." with no sign-in link.
  - In frontend/src/lib/api/questions.ts (`questionErrorMessage`) and
    frontend/src/lib/api/reviews.ts, handle `pilot_limit_reached` like `daily_limit_reached`:
    the server message plus "New questions can be asked after <local time>." or "New reviews can be
    requested after <local time>."
- [X] T027 [P] [US1] Create deploy/Caddyfile (research R3), for `caddy:2.10`:
  - A global block with `log default` using the same filter as below, so error logs are filtered
    too.
  - One site block for `{$CODEATLAS_HOSTNAME}`; `encode zstd gzip`; `header
    Strict-Transport-Security "max-age=31536000"`; a matcher `@api path /v1/* /auth/* /webhooks/*
    /healthz /readyz /version` reverse-proxied to `api:8000`; everything else to `web:3000`.
  - A `log` block writing JSON to stdout whose filter deletes the `code` and `state` query
    parameters of `request>uri` and of `resp_headers>Location`, and deletes
    `request>headers>Referer`.
  - Check it with `docker run --rm -e CODEATLAS_HOSTNAME=example.com -v "$PWD/deploy:/etc/caddy"
    caddy:2.10 caddy validate --config /etc/caddy/Caddyfile`.
- [X] T028 [US1] Create deploy/compose.yml (research R1, R2, R7, R9; contracts/operations.md
  "Host layout"):
  - `name: codeatlas`. It is run through `codeatlas-compose` with `--project-directory
    /opt/codeatlas`, so relative paths resolve there. Interpolation variables come from
    `/opt/codeatlas/.env`: `ENVIRONMENT`, `AWS_REGION`, `REGISTRY`, `CODEATLAS_HOSTNAME`,
    `IMAGE_TAG`.
  - `caddy` (`caddy:2.10`): ports 80 and 443, `./current/Caddyfile` mounted read-only, the
    `CODEATLAS_HOSTNAME` environment variable, `/var/lib/codeatlas/caddy` mounted at `/data`, and a
    named volume for `/config`.
  - `web` (`${REGISTRY}/codeatlas/web:${IMAGE_TAG}`): no ports.
  - `api` (`${REGISTRY}/codeatlas/api:${IMAGE_TAG}`): `uvicorn codeatlas.api.app:app --host
    0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "*"`, `env_file:
    ./runtime/app.env`, `./runtime/github-app.pem` mounted read-only at
    `/run/secrets/github-app.pem`, no ports, and a health check that requests `/healthz` with
    `python -c` and `urllib` (the image has no curl).
  - `worker`: the api image, `python -m codeatlas.jobs.worker`, the same env file and key mount,
    `stop_grace_period: 30s`.
  - `db` (`pgvector/pgvector:pg17`): `env_file: ./runtime/db.env`, data bind-mounted from
    `/var/lib/codeatlas/postgres`, `/var/lib/codeatlas/backup` mounted at `/backup`, `command:
    postgres -c shared_buffers=512MB -c max_connections=50`, health check `pg_isready`, no ports.
  - `migrate`: the api image, `alembic upgrade head`, the same env file and key mount (the full
    settings are validated when `alembic/env.py` loads them), `depends_on: db:
    condition: service_healthy`, `profiles: [tools]`, `restart: "no"`.
  - Every long-running service: `restart: unless-stopped`, and `logging: driver: awslogs` with
    `awslogs-region: ${AWS_REGION}`, `awslogs-group: /codeatlas/${ENVIRONMENT}/<service>`,
    `mode: non-blocking`, `max-buffer-size: 4m`. `api` and `worker` depend on `db` being healthy.
  - Check it with `docker compose -f deploy/compose.yml config --quiet`, with placeholder
    variables and empty `runtime/*.env` files.
- [X] T029 [P] [US1] Create deploy/render-config.sh `<environment>` (research R5;
  contracts/operations.md "SSM parameters"; `set -euo pipefail`, never `set -x`, never prints a
  value):
  - Read all parameters under `PARAMETERS_PATH` from `/opt/codeatlas/environment` with
    `aws ssm get-parameters-by-path --recursive --with-decryption`.
  - Decide the required parameters by mode: always `CODEATLAS_ENV`, `CODEATLAS_FAKE_EXTERNALS`,
    `TOKEN_ENCRYPTION_KEY`, and `POSTGRES_PASSWORD`; when `CODEATLAS_ENV=production`, also the
    GitHub and provider parameters. Fail naming each missing one.
  - Write `/opt/codeatlas/runtime/app.env` (mode 0600): every parameter except
    `GITHUB_APP_PRIVATE_KEY` and `POSTGRES_PASSWORD`, plus `APP_ORIGIN=https://$CODEATLAS_HOSTNAME`,
    `DATABASE_URL=postgresql+psycopg://codeatlas:<password>@db:5432/codeatlas`,
    `METRICS_ENVIRONMENT=$ENVIRONMENT`, `EMIT_METRICS=0` unless `ENVIRONMENT` is `pilot`, and,
    when the key exists, `GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/github-app.pem`. Write every
    value double-quoted, escaping `\`, `"`, and `$` (as `$$`); Compose's env-file parser rejects
    the shell's `'\''` form inside single quotes.
  - Write `runtime/db.env` (0600) with `POSTGRES_USER=codeatlas`, `POSTGRES_DB=codeatlas`, and
    `POSTGRES_PASSWORD`, and `runtime/github-app.pem` (0400, owner UID 1000 from T010) when the
    parameter exists.
  - Write each file to a temporary name and rename it, so a failure leaves the previous files.
- [X] T030 [P] [US1] Create deploy/put-secrets.sh `<environment>`, run on the developer's machine
  (research R5; `set -euo pipefail`):
  - Prompts with `read -rs` for each secret, or takes `--github-app-private-key FILE`; never
    echoes a value.
  - Generates `TOKEN_ENCRYPTION_KEY` (`openssl rand -base64 32 | tr '+/' '-_'`) and
    `POSTGRES_PASSWORD` (`openssl rand -hex 24`) only when they do not exist.
  - Writes the non-secret settings for the environment: pilot (`CODEATLAS_ENV=production`,
    `CODEATLAS_FAKE_EXTERNALS=0`, `EMIT_METRICS=1`); loadtest (`CODEATLAS_ENV=development`,
    `CODEATLAS_FAKE_EXTERNALS=1`, `DAILY_QUESTION_LIMIT=100000`, both pilot limits `100000`,
    `RATE_LIMIT_PER_MINUTE=0`, and no GitHub or provider secrets). `APP_ORIGIN` is never a
    parameter; render-config derives it. The drill writes no parameters; it reads the pilot's.
  - Uses `aws ssm put-parameter --type SecureString` for secrets and `String` otherwise; refuses
    to overwrite unless `--overwrite NAME` is given, for rotations. `--overwrite POSTGRES_PASSWORD`
    is refused with a pointer to the operations guide's `ALTER ROLE` procedure.
  - `--set NAME=VALUE` writes one optional setting (contracts/operations.md, "Limits and
    switches"), and `--unset NAME` deletes it, for temporary changes such as quickstart scenario 6.
- [X] T031 [US1] Create deploy/release.sh `<sha>` for releases by hand (research R7, "Host layout"
  and steps 1 to 4; T059 adds the check and rollback). It runs from its bundle directory
  `/opt/codeatlas/releases/<sha>/`, which the caller has extracted from
  `s3://<bucket>/releases/<sha>/deploy.tar.gz`:
  - Source `/opt/codeatlas/environment`; run `render-config.sh`; log in to ECR with
    `aws ecr get-login-password`.
  - With the new bundle's `compose.yml`, `--project-directory /opt/codeatlas`, and `IMAGE_TAG=<sha>`
    in the environment (it overrides the env file), pull only `api` and `web`, and run `migrate`.
    Any failure so far exits 2 without touching `/opt/codeatlas/.env`, `current`, or the running
    services (a trap maps every earlier error to 2).
  - Write `/opt/codeatlas/.env` with `IMAGE_TAG=<sha>` and the interpolation variables, copy
    `/var/lib/codeatlas/state/running` to `state/previous` (when it exists), point
    `/opt/codeatlas/current` at the bundle, and run `codeatlas-compose up -d --remove-orphans`.
    When the bundle's Caddyfile differs from the previous bundle's, also run `codeatlas-compose up
    -d --force-recreate caddy`.
  - Wait up to 120 seconds for `curl -fsS --resolve <hostname>:443:127.0.0.1
    https://<hostname>/readyz`.
  - Write `<sha>` to `/var/lib/codeatlas/state/running`, and append a line to
    `/var/log/codeatlas/releases.log`.
- [X] T032 [US1] Create `bats` tests in deploy/tests/ (research R18): `render_config.bats` and
  `release.bats`, with stub `aws`, `docker`, `curl`, and `codeatlas-compose` scripts placed first on
  `PATH` that record their arguments, and a temporary directory standing in for `/opt/codeatlas`
  and `/var/lib/codeatlas` (both scripts take these roots from `CODEATLAS_ROOT` and
  `CODEATLAS_DATA`, defaulting to the real paths):
  - render-config: a production parameter set writes `app.env` with the derived `APP_ORIGIN`,
    `DATABASE_URL`, and `METRICS_ENVIRONMENT`, `$` doubled, and modes 0600 and 0400; a missing
    production parameter fails naming it; a fake-mode set without GitHub parameters succeeds.
  - render-config writes values double-quoted with `\`, `"`, and `$` escaped, and forces
    `EMIT_METRICS=0` outside the pilot.
  - release: a successful release updates `current`, `.env`, and `state/running`, and recreates
    `caddy` only when the Caddyfile changed; a failing pull, login, or `migrate` exits 2, leaves
    `.env` unchanged, and never calls `up`.
- [X] T033 [US1] Create infra/shared/ (research R8; contracts/operations.md "Terraform"):
  - versions.tf: Terraform `>= 1.11`, AWS provider `~> 6.0`, `backend "s3"` with
    `use_lockfile = true`; the bucket and key come from `-backend-config=backend.hcl`
    (gitignored), with a `backend.hcl.example`.
  - variables.tf: `region` (default `us-east-1`), `domain`, `github_oidc_subject_prefix`,
    `alert_email`, `monthly_budget_usd` (default 40).
  - main.tf: ECR repositories `codeatlas/api` and `codeatlas/web` (`image_tag_mutability =
    "IMMUTABLE"`, scan on push, a lifecycle policy keeping the 20 newest images); the S3 bucket
    `codeatlas-<account ID>-<region>` with all public access blocked, SSE-S3, and one
    `aws_s3_bucket_lifecycle_configuration` (a bucket has only one; T053 adds a second rule to it)
    with a rule expiring `releases/` after 30 days; the Route 53 hosted zone for `domain`.
  - outputs.tf: `ecr_repositories`, `bucket`, `zone_id`, `name_servers`.
  - shared.tfvars.example with placeholder values.
- [X] T034 [US1] Create infra/stack/ (research R1 to R3, R8, R9):
  - versions.tf as in T033, with the state key `stack/<environment>.tfstate` from `backend.hcl`.
  - variables.tf: `environment` (validated to `pilot`, `drill`, or `loadtest`), `hostname`,
    `domain`, `availability_zone` (default `us-east-1a`), `instance_type` (default `t4g.medium`),
    `root_volume_gib` and `data_volume_gib` (default 20 each), `parameters_path` (default
    `/codeatlas/<environment>`).
  - main.tf:
    - the default VPC's subnet in `availability_zone`; the Ubuntu 24.04 `arm64` AMI from the SSM
      parameter
      `/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id`;
    - a security group allowing inbound TCP 80 and 443 from `0.0.0.0/0` and all outbound;
    - an instance role with `AmazonSSMManagedInstanceCore` and an inline policy: ECR pulls of the
      two repositories; `ssm:GetParametersByPath` on both `parameter<parameters_path>` and
      `parameter<parameters_path>/*`; `s3:GetObject` on `releases/*`; `logs:CreateLogStream`,
      `logs:PutLogEvents`, and `logs:DescribeLogStreams` on `/codeatlas/<environment>/*`; and
      `logs:DescribeLogGroups` on `*` (Run Command output);
    - the instance with `http_tokens = "required"`, `http_put_response_hop_limit = 1`,
      `credit_specification { cpu_credits = "standard" }`, an encrypted gp3 root volume, no key
      pair, `user_data` from `cloud-init.yaml.tftpl`, `user_data_replace_on_change = true`,
      `lifecycle { ignore_changes = [ami] }`, and the tag `codeatlas:environment = <environment>`;
    - two encrypted gp3 data volume resources in the same zone, one per kind with `count`:
      `pilot_data` (pilot only, `lifecycle { prevent_destroy = true }`) and `temporary_data`
      (drill and loadtest, unprotected); an attachment as `/dev/sdf` with
      `stop_instance_before_detaching = true`;
    - an Elastic IP associated with the instance.
  - dns.tf: an `A` record for `hostname` to the Elastic IP, TTL 300, in the shared zone (looked up
    by `domain`).
  - logs.tf: log groups `/codeatlas/<environment>/{caddy,web,api,worker,db,releases,host}` with
    7-day retention.
  - outputs.tf: `instance_id`, `public_ip`, `url`.
  - pilot.tfvars.example, drill.tfvars.example (with `parameters_path = "/codeatlas/pilot"`), and
    loadtest.tfvars.example.
- [X] T035 [US1] Create infra/stack/cloud-init.yaml.tftpl (research R2, R7):
  - Install Docker Engine and the Compose plugin from Docker's apt repository, the AWS CLI v2 for
    `aarch64`, `jq`, and `unzip`.
  - Write `/opt/codeatlas/environment` with `ENVIRONMENT`, `AWS_REGION`, `REGISTRY`, `BUCKET`,
    `CODEATLAS_HOSTNAME`, and `PARAMETERS_PATH`, and install `/usr/local/bin/codeatlas-compose`
    (contracts/operations.md "Host layout").
  - Wait up to 5 minutes for `/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_<volume ID without
    the dash>`; create ext4 only if `blkid` finds no file system; mount it by UUID at
    `/var/lib/codeatlas` with `nofail`; create `postgres/`, `caddy/`, `backup/`, and `state/` on it.
  - Write `/etc/systemd/system/docker.service.d/codeatlas.conf` with `[Unit]` and
    `RequiresMountsFor=/var/lib/codeatlas`, then run `systemctl daemon-reload` and restart Docker.
  - Create a 2 GiB swap file; set `Unattended-Upgrade::Automatic-Reboot "true"` and
    `Unattended-Upgrade::Automatic-Reboot-Time "04:30"`; create `/var/log/codeatlas`.
- [X] T036 [US1] Write docs/operations.md, first sections (FR-030; contracts/operations.md):
  - One-time setup: tools, the state bucket (T003), `infra/shared` (plan, review, apply the saved
    plan), delegating the domain to the zone's name servers, the pilot GitHub App (permissions
    Contents, Metadata, and Pull requests read-only; events Push, Installation, and Installation
    repositories; callback `https://<hostname>/auth/github/callback`; webhook
    `https://<hostname>/webhooks/github` with its secret; public), `put-secrets.sh pilot`, and
    `infra/stack` with `pilot.tfvars`.
  - Releasing by hand: build both images for a commit with `docker buildx build --platform
    linux/arm64` and the build arguments, push them to ECR unless the tag already exists (tags are
    immutable), upload `deploy/` from a checkout of that commit as `releases/<sha>/deploy.tar.gz`
    unless it exists (bundles expire after 30 days), and run the SSM command that extracts it into
    `/opt/codeatlas/releases/<sha>/` and runs its `release.sh <sha>`. The drill, the load test, and
    a replaced host use the same procedure for an existing commit.
  - Temporary settings: `put-secrets.sh pilot --set NAME=VALUE`, release the running commit again,
    and `--unset` it afterward.
  - Managing pilot users and deleting a removed user's data (`codeatlas-compose run --rm api
    python -m codeatlas.ops ...` through Session Manager).
  - Replacing a credential (until T059: `put-secrets.sh --overwrite`, then release the running
    commit again), including the effect of replacing `TOKEN_ENCRYPTION_KEY`, and the database
    password: `ALTER ROLE codeatlas PASSWORD '...'` through `codeatlas-compose exec db psql`,
    then the parameter (written with `aws ssm put-parameter --overwrite`), then the release.
  - Tearing down: `terraform destroy` per stack (the pilot's data volume refuses until its
    `prevent_destroy` is removed deliberately; drill and loadtest destroy cleanly), then
    `infra/shared`.
- [X] T037 [US1] Extend .github/workflows/ci.yml:
  - A new `infra` job: `terraform fmt -check -recursive infra`; `terraform init -backend=false`
    and `terraform validate` in `infra/shared` and `infra/stack`; `shellcheck deploy/*.sh`;
    install `bats` and run `bats deploy/tests`; create placeholder `runtime/app.env` and
    `runtime/db.env` and run `docker compose -f deploy/compose.yml config --quiet` with placeholder
    variables; and the Caddy check from T027.
  - Build the images in the `backend` and `frontend` jobs, whose steps run in those directories:
    `docker build .` and `docker build --target production .`.
- [ ] T038 [US1] Developer action: build the pilot and validate User Story 1 (needs T003):
  - Follow docs/operations.md: apply `infra/shared`, delegate the domain, register the pilot
    GitHub App, run `put-secrets.sh pilot`, apply `infra/stack` with `pilot.tfvars`, release the
    current `main` commit by hand, and add the pilot users.
  - Run quickstart.md scenarios 1 to 7, 9, and 19 and record the results in its validation record.

**Checkpoint**: CodeAtlas is live for invited users. This is the MVP.

---

## Phase 4: User Story 2 - Learn about failures and recover from them (Priority: P2)

**Goal**: Alerts by email for outages, a stopped or lagging worker, failing jobs, low disk, missing
backups, certificate expiry, and spending; logs and metrics in one place; nightly backups and a
rehearsed recovery.

**Independent Test**: Run the quickstart's fault tests and the recovery exercise: each fault's
alert arrives within 10 minutes with a recovery notice, and a drill environment restored from the
latest backup shows a pilot user's data within 2 hours.

### Tests for User Story 2 ⚠️ (write first, confirm they fail)

- [X] T039 [P] [US2] Unit tests for EMF lines in backend/tests/unit/test_metrics.py (research
  R10), capturing stdout with `capsys`:
  - With `emit_metrics` on and `metrics_environment` `pilot`, `emit({"QueuedJobs": 3,
    "OldestRunnableJobAgeSeconds": 42.0})` writes one JSON line with `_aws.Timestamp` (integer
    milliseconds), `_aws.CloudWatchMetrics[0].Namespace == "CodeAtlas"`,
    `Dimensions == [["Environment"]]`, a definition per metric with its unit (`Count` or
    `Seconds`), the values at the top level, and `"Environment": "pilot"`.
  - With `emit_metrics` off, nothing is written.
- [X] T040 [P] [US2] Integration tests for worker metrics and job lines in
  backend/tests/integration/test_worker_metrics.py, with `emit_metrics` on through the `settings`
  fixture. Capture EMF with `capsys` and log lines with the custom handler that
  backend/tests/integration/test_logging.py uses (`_Capture`), because `configure_logging` binds
  its stream at creation:
  - `metrics.report_once()` reports `QueuedJobs` (jobs `queued` or `retry_wait`),
    `OldestRunnableJobAgeSeconds` (now minus `run_after` of the oldest job `claim_next` could claim
    now; a `retry_wait` job with a future `run_after`, and a job whose workspace already runs a job
    of the same kind, are ignored; 0 when none), and `WorkerHeartbeat` 1, with the `Environment`
    from `metrics_environment`.
  - The reporter thread started by `metrics.start_reporter()` keeps reporting while a job handler
    blocks the job loop (use a 0.1-second interval in the test), and stops when its event is set.
  - A job that succeeds logs a `job_finished` line with `kind`, `outcome` `succeeded`, `attempt`,
    and `duration_ms`.
  - A retryable failure logs `outcome` `retry_wait` and emits no `JobsFailed`.
  - A failure with no retry left, a timeout, and an `internal_error` each log `outcome` `failed`
    and emit `JobsFailed` 1, including a job failed by `claim_next` (expired deadline), whose
    `job_finished` line still carries its `job_id`. A permanent input failure (for example a size
    limit) logs `failed` and emits no `JobsFailed`.
- [X] T041 [P] [US2] Unit tests for backups in backend/tests/unit/test_backup_manifest.py (data
  model, "Backup manifest"):
  - `build_manifest(counts, release=..., dump_key=..., bytes_=..., sha256=..., created_at=...)`
    returns the documented shape from a counts document (`alembic_revision`, `row_counts`).
  - Invalid counts documents (missing keys, non-integer counts) raise a clear error.
  - `compare(manifest, row_counts, revision)` returns no differences for equal input, and one
    readable difference each for a count mismatch, a missing table, an extra table, and a revision
    mismatch.
- [X] T042 [P] [US2] Extend backend/tests/integration/test_ops_commands.py for backups: write a
  counts file with the current row counts and `alembic_revision` `0004`; `backup-manifest --counts
  <file> --dump-key K --bytes 10 --sha256 <hex>` prints the manifest JSON; `verify-restore <that
  file>` with the printed manifest saved to a file exits 0; after inserting one row into
  `audit_events` it exits 1 and prints the table.
- [X] T043 [P] [US2] Extend backend/tests/integration/test_logging.py: a `GET
  /v1/repositories/{id}` produces one `request` line with `method`, `route`
  (`/v1/repositories/{repository_id}`, the template with its prefix), `status`, `duration_ms`, and
  `request_id`, and no query string or body content; a route that raises an unexpected exception
  produces a `request` line with status 500.
- [X] T044 [P] [US2] Extend backend/tests/unit/test_gemini_answer_model.py and
  backend/tests/unit/test_voyage_embedder.py: a retried error, a non-retryable error (for example
  401), a blocked response, and exhausted retries are each logged with the structured field
  `provider` (`gemini` or `voyage`) and no request content.

### Implementation for User Story 2

- [X] T045 [US2] Create backend/src/codeatlas/metrics.py:
  - `emit(values, *, units=None)` writes EMF lines to `sys.stdout` (flushed) only when
    `get_settings().emit_metrics`, with `Environment` from `metrics_environment`. Units default to
    `Count`, and to `Seconds` for names ending in `Seconds`.
  - `report_once()` queries the three queue values of T040 in its own session, using the same
    eligibility conditions as `claim_next`'s `_claimable` query, and emits them.
  - `start_reporter(interval=60) -> threading.Event` starts a daemon thread that calls
    `report_once()` every interval until the returned event is set, logging and surviving errors.
- [X] T046 [US2] Job outcomes and failure metrics:
  - In backend/src/codeatlas/jobs/queue.py, make `fail` return the job's final status
    (`retry_wait` or `failed`). In `_mark_failed`, which both `fail` and `claim_next` reach, inside
    `log_context(job_id=str(job.id))`, log `job_finished` with `outcome` `failed`, and emit
    `JobsFailed` 1 when the failure is not
    permanent (retries used up), is a timeout from `timeout_failure`, or has code `internal_error`.
  - In backend/src/codeatlas/jobs/worker.py, make `_record_failure` return `fail`'s status; in
    `_run`, time the attempt and log `job_finished` with `kind`, `outcome` (`succeeded` or
    `retry_wait`), `attempt`, and `duration_ms` (the failed outcome is logged by `_mark_failed`).
    In `main`, when `emit_metrics` is set, call `metrics.start_reporter()` before the loop and set
    its event when stopping.
- [X] T047 [US2] Request log line in backend/src/codeatlas/api/app.py: a middleware registered
  before `request_ids` (so it runs inside it and sees the request ID) times the request and logs
  `request` through the `codeatlas.request` logger with `method`, `route`, `status`, and
  `duration_ms`. Take `route` from `request.scope.get("route")` after `call_next` returns, or in
  `finally` when it raises: the router sets it on the same scope, without the include prefix, so
  prefix `/v1` when the request path starts with `/v1/`; log `unmatched` when there is no route.
  Do not search `app.router.routes`: in FastAPI 0.142 it holds `_IncludedRouter` entries without
  `path` (research R9). On an exception, log status 500 and re-raise.
- [X] T048 [US2] Provider field on error logs: in backend/src/codeatlas/providers/answer_model.py
  and backend/src/codeatlas/providers/embeddings.py, log a warning with
  `extra={"fields": {"provider": "gemini"}}` or `"voyage"` for each retried error and before each
  raised error (blocked, rejected, and retries exhausted), without request content.
- [X] T049 [US2] Backups in the operator commands:
  - Create backend/src/codeatlas/ops/backup.py with the pure `build_manifest` and `compare`.
  - Add `backup-manifest --counts FILE --dump-key --bytes --sha256` (reads the counts file; the
    `release` comes from settings) and `verify-restore MANIFEST` (counts every table in the
    `public` schema and reads `alembic_version`, in one `REPEATABLE READ` transaction) to
    backend/src/codeatlas/ops/__main__.py.
- [X] T050 [P] [US2] Host scripts (research R12; contracts/operations.md "Host scripts";
  `set -euo pipefail`; each logs to `/var/log/codeatlas/<script>.log`). Write their `bats` tests
  first, in deploy/tests/host_scripts.bats with stub `psql`, `docker`, `aws`, and `openssl`
  (research R18): `backup.sh` publishes no metric and exits non-zero when the dump step fails;
  `restore.sh` refuses a dump whose checksum does not match; `check-certificate.sh` publishes the
  days left computed from a given end date.
  - deploy/backup.sh:
    - In the `db` container, one `psql -v ON_ERROR_STOP=1` session: `BEGIN ISOLATION LEVEL
      REPEATABLE READ`; `SELECT pg_export_snapshot() AS snapshot \gset`; `\setenv
      CODEATLAS_SNAPSHOT :snapshot`; `\! pg_dump -Fc --snapshot="$CODEATLAS_SNAPSHOT" -f
      /backup/codeatlas.dump -U codeatlas codeatlas`; then `\if :SHELL_ERROR` raises an error
      (for example `DO $$BEGIN RAISE EXCEPTION 'pg_dump failed'; END$$;`) and `\endif`, because
      psql counts a `\!` command as successful whatever its exit status; then one query that
      returns the row count of every table in the `public` schema and
      `alembic_version.version_num` as one JSON value, written with `\g (format=unaligned
      tuples_only=on) /backup/counts.json`; `COMMIT`.
    - On the host, check the dump with `pg_restore --list` (in the `db` container); compute its
      size and SHA-256; run `codeatlas-compose run --rm -v /var/lib/codeatlas/backup:/backup:ro api
      python -m codeatlas.ops backup-manifest --counts /backup/counts.json ...` to build
      `manifest.json`; upload both
      files to `s3://$BUCKET/backups/<UTC date>/` with `aws s3api put-object --if-none-match '*'`;
      delete the staged files; publish `BackupCompleted` 1 with `aws cloudwatch put-metric-data
      --namespace CodeAtlas --dimensions Environment=$ENVIRONMENT`.
  - deploy/restore.sh `<UTC date>`: download the dump and manifest into
    `/var/lib/codeatlas/backup`; check the SHA-256; stop `api` and `worker`; `pg_restore --clean
    --if-exists --no-owner --exit-on-error` into `db`; run `verify-restore
    /backup/manifest.json` with the same `-v` mount; leave `api` and `worker` stopped for the
    operator.
  - deploy/check-certificate.sh: read the certificate's end date with `openssl s_client
    -servername $CODEATLAS_HOSTNAME -connect 127.0.0.1:443`, and publish `CertificateDaysLeft`.
  - deploy/systemd/: `codeatlas-backup.service` (`ExecStart=/opt/codeatlas/current/backup.sh`)
    and `.timer` (`OnCalendar=*-*-* 03:30:00 UTC`, `Persistent=true`), and
    `codeatlas-cert-check.service` (`ExecStart=/opt/codeatlas/current/check-certificate.sh`) and
    `.timer` (daily at 06:00 UTC).
- [X] T051 [P] [US2] Create deploy/cloudwatch-agent.json: `mem_used_percent`, and
  `disk_used_percent` with `resources` limited to `/` and `/var/lib/codeatlas`, `drop_device:
  true`, every 60 seconds, with `append_dimensions` `InstanceId` (so the disk metric's dimensions
  are `InstanceId`, `path`, and `fstype`); and the files `/var/log/codeatlas/*.log` shipped to
  `/codeatlas/<environment>/host`.
- [X] T052 [US2] Extend infra/stack/cloud-init.yaml.tftpl: install the CloudWatch agent's `arm64`
  package from AWS, write its configuration with the environment's values, and start it; in the
  pilot only, install the units from `deploy/systemd/` (shipped in the user data) and enable both
  timers. Because `user_data_replace_on_change` is set, the plan shows a host replacement (T057).
- [X] T053 [US2] Extend infra/shared/: the SNS topic `codeatlas-alerts` with an email subscription
  to `alert_email`; the budget `codeatlas-monthly` for `monthly_budget_usd` with email
  notifications at 80% and 100% of actual spend and 80% of forecast spend (forecasts start after
  about five weeks of data); a second rule in T033's lifecycle configuration expiring `backups/`
  after 7 days; output `alert_topic_arn`.
- [X] T054 [US2] Extend infra/stack/:
  - Instance role: `s3:GetObject` on `backups/*` in the pilot (which restores into itself after
    losing its data volume) and the drill, not the load test; in the pilot, `s3:PutObject` on
    `backups/*` only with the condition `s3:if-none-match` equal to `*` (no delete or overwrite;
    confirm the condition with the IAM policy simulator); `cloudwatch:PutMetricData` limited by the
    `cloudwatch:namespace` condition to `CWAgent` in every environment, and also `CodeAtlas` in the
    pilot.
  - monitoring.tf, created only when `environment == "pilot"`: Route 53 HTTPS health checks of
    `hostname` on port 443 for `/readyz` and `/`, every 30 seconds, failure threshold 3; the ten
    alarms of research R10, with `alarm_actions` and `ok_actions` set to the `codeatlas-alerts`
    topic (looked up by name); `treat_missing_data = "breaching"` for `worker-down`,
    `backup-missing`, and `certificate-expiring`; for `backup-missing` (Sum below 1) and
    `certificate-expiring` (Minimum below 14), `period = 3600`, `evaluation_periods = 26`, and
    `datapoints_to_alarm = 26`; `jobs-waiting` at 1,800 seconds; disk alarm dimensions
    `InstanceId`, `path`, and `fstype = ext4`; and the dashboard `codeatlas-pilot` with metric
    widgets and Logs Insights widgets for request count, 5xx count, and p95 `duration_ms` by route
    group, job `duration_ms`, retries, and failures by `kind`, and provider errors by
    `provider`.
- [X] T055 [P] [US2] Disclosure (FR-018): add "Deleted data can remain in encrypted backups for up
  to 10 days after deletion." to frontend/src/components/ExternalProcessingAcceptance.tsx, after the
  processing sentence.
- [X] T056 [US2] Extend docs/operations.md: confirming the alert subscription; the dashboard;
  saved Logs Insights queries (all lines for a `request_id` or `job_id` across the log groups,
  p95 by route, failed jobs); backups; restoring; the recovery exercise with the drill (research
  R12, including adding the drill's callback URL to the App and starting it without `worker`);
  replacing the host (after a bootstrap change or a host failure: apply with `-replace`, then
  release the commit in `state/running`); losing the data volume (state removal, new volume,
  release, restore, and telling pilot users the backup time); and the fault tests with
  `set-alarm-state` for the alarms they do not trigger.
- [ ] T057 [US2] Developer action: apply the changes to `infra/shared`, confirm the subscription,
  apply the pilot stack (the plan replaces the host, so the new bootstrap installs the agent and
  timers), release the running commit to the new host, and then:
  - run the quickstart's fault tests and alarm notification checks (SC-007) and record each
    alert's delay;
  - run the recovery exercise (SC-008) and record its duration; and
  - run quickstart scenarios 8, 10, 11, and 17 on the drill or pilot as written, and record them.

**Checkpoint**: The developer learns about failures and can recover from losing the host.

---

## Phase 5: User Story 3 - Release changes safely (Priority: P3)

**Goal**: Pushes to `main` become release candidates with a summary; nothing is written to AWS
until the developer approves; a failed check rolls back automatically and alerts.

**Independent Test**: Merge a visible change, approve it, and see the new commit within 15
minutes; dispatch a release with `expect_version=wrong` and see the previous commit restored within
5 minutes with an alert.

### Tests for User Story 3 ⚠️ (write first, confirm they fail)

- [X] T058 [P] [US3] Extend deploy/tests/release.bats (research R18): with the stub `curl`
  failing the check, the previous bundle is started again, `current` points back at it, and the
  script exits 3; with no `state/running` (first release on a host) it stops the new services and
  exits 4; when the restart of the previous release also fails, it exits 4; `--expect-version V`
  changes the expected `/version`; `--refresh-config` re-renders configuration and restarts only
  `api` and `worker`.

### Implementation for User Story 3

- [X] T059 [US3] Complete deploy/release.sh (research R7, steps 5 and 6; contracts/operations.md):
  - After `up -d`, check for up to 120 seconds that `/readyz` and `/` return 200 and `/version`
    returns the expected commit (`--expect-version V` overrides it) through
    `curl --resolve <hostname>:443:127.0.0.1`.
  - On failure, point `current` at the bundle of `state/previous`, write `.env` with its tag, start
    it (recreating `caddy` when the Caddyfiles differ), wait for readiness, restore
    `state/running`, and exit 3; exit 4 if that fails, or if there is no previous release, after
    stopping the new services.
  - `--refresh-config`: re-render configuration and run `codeatlas-compose up -d --force-recreate
    api worker` on the running tag (a restart would keep the old environment).
  - Append the outcome to `/var/log/codeatlas/releases.log`.
- [X] T060 [US3] Extend infra/shared/ with GitHub's OIDC provider
  (`token.actions.githubusercontent.com`, audience `sts.amazonaws.com`) and one role,
  `codeatlas-release`, trusted only when `sub` equals
  `${var.github_oidc_subject_prefix}:environment:pilot`, allowed:
  - `ecr:GetAuthorizationToken`, and the push and describe actions on the two repositories;
  - `s3:PutObject` on `releases/*`;
  - `ssm:SendCommand` in two statements: on instances with the condition
    `ssm:resourceTag/codeatlas:environment` equal to `pilot`, and on the `AWS-RunShellScript`
    document;
  - `ssm:GetCommandInvocation` and `ssm:ListCommandInvocations`; and
  - `sns:Publish` on `codeatlas-alerts`.
  - Output `release_role_arn`; document in shared.tfvars.example how to read the prefix with
    `gh api repos/<owner>/<repo>/actions/oidc/customization/sub`.
- [X] T061 [US3] Create .github/workflows/release.yml (contracts/operations.md, "Release
  workflow"; research R7):
  - Triggers: `workflow_run` of `CI` (`types: [completed]`, `branches: [main]`), and
    `workflow_dispatch` with `commit` and `expect_version`. Workflow `permissions: contents: read`;
    the `summary` job adds `actions: read`, and only the `release` job adds `id-token: write`.
    `concurrency: release-pilot` is set on the `release` job only, so runs whose jobs are skipped
    never join the group. Settings come from repository variables
    (`AWS_REGION`, `RELEASE_ROLE_ARN`, `RELEASE_BUCKET`, `PILOT_HOSTNAME`, `ALERT_TOPIC_ARN`,
    `ECR_REGISTRY`).
  - Gate every job with `if: github.event_name == 'workflow_dispatch' ||
    (github.event.workflow_run.conclusion == 'success' && github.event.workflow_run.event ==
    'push' && github.event.workflow_run.head_branch == 'main' &&
    github.event.workflow_run.head_repository.full_name == github.repository)`. The commit is the
    event's `head_sha` (or the dispatch input); every checkout uses it.
  - `build`: a matrix of `api` and `web` on `ubuntu-24.04-arm`, with no cloud credentials; build
    with `CODEATLAS_RELEASE` or `NEXT_PUBLIC_RELEASE` set to the commit, tag
    `<registry>/codeatlas/<name>:<sha>`, `docker save | gzip`, and upload as an artifact kept 3
    days.
  - `summary`: check out with full history; on dispatch, fail unless `git merge-base
    --is-ancestor <sha> origin/main` succeeds and `gh run list --workflow CI --commit <sha>`
    shows a successful run; read `https://$PILOT_HOSTNAME/version` (none on a first release);
    write the commit, `git log --oneline <running>..<sha>`, and `git diff --name-only
    --diff-filter=A <running> <sha> -- backend/alembic/versions` to `$GITHUB_STEP_SUMMARY`,
    schema changes first with the expand-then-contract reminder.
  - `release`: `runs-on: ubuntu-24.04` (pinned, research R7); `environment: pilot`; needs `build`
    and `summary`; after approval, assume the
    release role; download and `docker load` both artifacts and push each tag unless
    `aws ecr describe-images` finds it; upload `deploy/` as `releases/<sha>/deploy.tar.gz`; send
    the Run Command that extracts it into `/opt/codeatlas/releases/<sha>/` and runs `release.sh
    <sha>` (with `--expect-version` when given), with output to `/codeatlas/pilot/releases`;
    meanwhile probe `/readyz` and `/` once a second and record the longest run of failures of
    either; poll
    `get-command-invocation` until it ends (at most 20 minutes); write the outcome and the longest
    outage to the summary; on any failure publish the commit and exit status to
    `ALERT_TOPIC_ARN`.
- [ ] T062 [US3] Developer action: read the OIDC subject prefix and add it to `shared.tfvars`;
  apply the OIDC changes to `infra/shared`; create the GitHub environment `pilot` with the
  developer as required reviewer and only `main` allowed; set the repository variables of T061.
- [X] T063 [US3] Extend docs/operations.md: setting up releases (reading the OIDC subject prefix,
  applying `infra/shared`, creating the `pilot` environment, and the repository variables); the
  release flow and the summary; rejecting a stale
  run that waits for approval (it blocks newer runs); the expand-then-contract rule for
  migrations; rolling back by hand (`release.sh <previous sha>`); the forced-rollback rehearsal;
  and replacing credentials with `release.sh --refresh-config`.
- [ ] T064 [US3] Developer action: validate User Story 3 with quickstart scenarios 12 to 16 and
  18; record five releases' approval-to-serving times and longest outages (SC-009) and the forced
  rollback's recovery time (SC-010). If one of the releases includes a migration, rehearse the
  rollback on it and record that the previous version works with the new schema (US3-4);
  otherwise record that this check waits for the first release with a migration.

**Checkpoint**: Releases are repeatable, approved, checked, and reversible.

---

## Phase 6: User Story 4 - Publish measured results (Priority: P4)

**Goal**: A load test report on the pilot's hardware, an evaluation report with the benchmark and
the project's own sets, and a README with a demo, a diagram, limitations, and links.

**Independent Test**: Rerun the load test workflow from its report's instructions and get a report
of the same form; every number in both reports has its denominator, commit, and configuration.

### Tests for User Story 4 ⚠️ (write first, confirm they fail)

- [X] T065 [P] [US4] Unit tests in backend/tests/unit/test_perf_check.py: the summary that
  `--json` writes has `base_url`, `commit`, `users`, `duration`, and per category `count`,
  `errors`, `p50`, `p95`, `limit`, and `passed`, plus an overall `passed`; and
  backend/evals/load_report.py renders several level files into one Markdown table, in level
  order, marking the highest level that passes, or "at least <highest level>" when all pass.
- [X] T066 [P] [US4] Unit tests in backend/tests/unit/test_bench_eval.py (research R16), with small
  fixtures copied from the pinned benchmark commit:
  - Parsing a golden-comments file (a list of `{pr_title, url, comments: [{comment, severity,
    category}], ...}` with optional `original_url` and `az_comment`) keeps every item, keyed by its
    `url`, and parses the URL into owner, repository, and number.
  - The export writes, for the tool `codeatlas`, one review entry per golden URL in the structure
    of `results/benchmark_data.json`, and one candidate per risk in the structure of
    `candidates.json`, whose text includes the risk's file and line range.
  - The comparison sums another tool's true positives, false positives, and false negatives from a
    fixture `evaluations.json` over the selected golden URLs, and computes precision and recall.
  - Items with an `az_comment` are flagged, and the comparison can be computed with and without
    them.

### Implementation for User Story 4

- [X] T067 [P] [US4] Load test tooling:
  - Add `--json PATH` to backend/evals/perf_check.py, writing T065's summary; read `commit` from
    `GET /version`.
  - Create backend/evals/load_report.py (level JSON files in, Markdown table out).
  - Create .github/workflows/load-test.yml: `workflow_dispatch` with `base_url`, `levels`
    (default `10,20,40,80`), and `duration` (default 120); on `ubuntu-24.04`, measure the median
    `time_connect` and `time_appconnect` of 20 `curl` calls to `/healthz`; run `perf_check` per
    level with `--origin` equal to `base_url`, stopping after the first failing level; render the
    table; upload the JSON files and the table as an artifact.
- [ ] T068 [US4] Developer action (needs T051 and T052 for CPU and memory): run the load test
  (FR-026, SC-011): apply `infra/stack` with `loadtest.tfvars`, run `put-secrets.sh loadtest`,
  release the pilot's commit by hand, dispatch `load-test.yml`, read peak CPU and memory per level
  from CloudWatch, and destroy the stack. Then write docs/reports/load-test.md from the artifact
  (an agent may draft it): the steps and inputs to rerun it (workflow inputs, variable file,
  commit), hardware, configuration, duration, the network baseline, each level's p95 and error
  rate by category, the highest level meeting every target (or "at least 80"), and peak CPU and
  memory. If peak memory stays under half the host, record the `t4g.small` decision in ADR 0010.
  Add a "Running the load test" section to docs/operations.md with these steps.
- [X] T069 [P] [US4] Create backend/evals/run_bench_eval.py (FR-027, research R16), with options
  `--bench-commit` (required), `--out` (default `evals/out/bench`), `--limit`, `--judge-model`
  (default `claude-opus-4-5-20251101`), `--exclude-flagged`, and `--skip-judge`:
  - Download the benchmark at the pinned commit into `--out` from codeload, and read
    `offline/golden_comments/sentry.json` and `cal_dot_com.json`.
  - Resolve each pull request's base and head commits and merge base with GitHub's REST API,
    following redirects (`calcom/cal.com` is now `calcom/cal.diy`), using `GITHUB_TOKEN`
    (read-only) for the rate limit.
  - Download archives with `run_review_eval.download_archive`, read them with `read_tree` under a
    copy of the settings whose snapshot and archive limits are raised (record the values), and
    call `analyze` with the real model, as `run_review_eval.py` does.
  - Write the `codeatlas` entries into the benchmark's `offline/results/benchmark_data.json` and
    into `candidates.json` under `offline/results/<MARTIAN_MODEL with "/" replaced by "_">/`, the
    directory the benchmark's steps use, keyed by golden URL.
  - Unless `--skip-judge`, run the benchmark's step 2.5 and step 3 with `--tool codeatlas`
    through `uv run --directory <benchmark>/offline python -m code_review_benchmark...`, with
    `MARTIAN_API_KEY`, `MARTIAN_BASE_URL`, and `MARTIAN_MODEL` set for the judge (Anthropic's
    OpenAI-compatible endpoint for Claude Opus 4.5, or OpenAI for GPT-5.2). Do not pass
    `--dedup-groups`; step 3 finds the file itself.
  - Read the published results of the same judge from its published directory
    (`offline/results/anthropic_claude-opus-4-5-20251101/` or `offline/results/openai_gpt-5.2/`).
  - Compute CodeAtlas's precision and recall and every published tool's on the same golden URLs,
    with and without the flagged items; write `bench-eval-<time>.md` and `.json` with the benchmark
    commit, judge, limits, per-item results, cost, and duration.
  - Exit codes: 0 on a completed run, 2 on setup errors.
- [X] T070 [US4] Extend backend/evals/README.md: a "Code Review Bench" section (keys, cost, the
  pinned commit, the judge order, checking the providers' deprecation pages before a run, the
  results directories, the raised limits, and the caveats in research R16), and the review-status
  labels that reports must use for `qa_v1.jsonl` and `review_v2.jsonl` (FR-028).
- [ ] T071 [US4] Developer action: run the three evaluations (quickstart.md, "Evaluation") with the
  developer's keys, the benchmark first and before its published judges are retired. Then write
  docs/reports/evaluation.md from their outputs (an agent may draft it): the benchmark table next
  to the published tools (with and without the flagged items), the 001 and 003 criteria from
  FR-028 with denominators, per-item failures, cost and latency per item, commits, set versions
  and SHA-256, the review-status labels, and the deferred human audits (SC-012).
- [ ] T072 [US4] Update README.md (FR-029, FR-031; research R17):
  - At the top, `docs/assets/demo.gif`: at most 60 seconds and 10 MB, recorded by the developer on
    the pilot (connect a repository, ask where something is implemented, open the cited code,
    review a pull request). Developer action for the recording.
  - A Mermaid architecture diagram of the deployed system: browser and GitHub webhooks to Caddy;
    Caddy to the web app and the API; the API and the worker to PostgreSQL; the worker to GitHub,
    Gemini, and Voyage; host scripts to S3 and CloudWatch; GitHub Actions to ECR and SSM.
  - A "Known limitations" section with the four limitations of FR-031.
  - A data section with the 7-day log retention and the up-to-10-day backup retention of deleted
    data.
  - Links to docs/reports/load-test.md, docs/reports/evaluation.md, and docs/operations.md.

**Checkpoint**: The measured results are published.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Log hygiene, documentation links, scans, and final validation

- [X] T073 [P] Extend backend/tests/integration/test_logging.py: a denied sign-in, each operator
  command, and a rate-limited request log no token, cookie, OAuth `code` or `state`, or webhook
  secret.
- [X] T074 [P] Documentation:
  - In README.md, describe the pilot deployment, link specs/004-pilot-deployment (spec, plan,
    research, data model, both contracts, and quickstart), and add ADRs 0010 to 0013 to the
    decision list.
  - Extend the superseded-in-part note at the top of docs/design/codeatlas-v1.md: for
    `specs/004-pilot-deployment`, ADRs 0010 to 0013 apply; the pilot runs on one EC2 host without
    ECS, RDS, SQS, or a staging environment (research, deviations table).
- [ ] T075 Developer action: credential scan (SC-006): `gitleaks git` over the repository history;
  `gitleaks dir` over both images' exported file systems; and `gitleaks dir` over an export of 7
  days of the pilot's log groups. Record the results in the quickstart's validation record (the
  port scan is scenario 9).
- [ ] T076 Run final validation:
  - Every automated check in quickstart.md.
  - Every validation scenario not yet recorded.
  - Scan the changes for absolute local paths, secrets, personal notes, email addresses, and
    mentions of private material before committing; real `*.tfvars` and `backend.hcl` files stay
    untracked.
  - Fix any failure until every test passes, and complete the quickstart's validation record.
- [ ] T077 Developer action, after launch: after 14 days, record the `/readyz` check's average
  `HealthCheckPercentageHealthy` for the period (SC-001); after the first full month, record the
  hosting cost from Cost Explorer (SC-002); record the highest daily model spend in that month
  (SC-003).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies. T003 is a developer action needed by T038.
- **Foundational (Phase 2)**: depends on T002. It blocks every story.
- **User Story 1 (Phase 3)**: depends on Foundational. T038 needs every other US1 task.
- **User Story 2 (Phase 4)**: depends on User Story 1 (it monitors and backs up the running
  pilot). T057 needs T045 to T056.
- **User Story 3 (Phase 5)**: depends on User Story 1 (release.sh, the stack, and the shared
  module) and on T053 (the alert topic that T060 and T061 use). It does not need the rest of
  User Story 2.
- **User Story 4 (Phase 6)**: the load test (T068) needs User Story 1 and the CloudWatch agent
  from T051 and T052; the README's diagram and limitations describe User Stories 1 to 3. The
  benchmark and evaluation tasks (T066, T069 to T071) need only Foundational and should start
  early, before the published judges are retired (research R16).
- **Polish (Phase 7)**: depends on every story. T077 runs weeks after launch.

### Shared files (do these tasks sequentially)

- backend/src/codeatlas/config.py: T002, then T019.
- backend/tests/unit/test_config.py: T002, then T012.
- backend/src/codeatlas/api/app.py: T009, then T020, then T047.
- backend/src/codeatlas/ops/__main__.py: T024, then T049.
- backend/tests/integration/test_ops_commands.py: T016, then T042.
- backend/tests/integration/test_logging.py: T043, then T073.
- backend/evals/perf_check.py: T002, then T067.
- deploy/release.sh: T031, then T059.
- deploy/tests/release.bats: T032, then T058.
- infra/shared/: T033, then T053, then T060.
- infra/stack/ and its cloud-init template: T034 and T035, then T052 and T054.
- docs/operations.md: T036, then T056, then T063.
- README.md: T072, then T074.
- .github/workflows/ci.yml: T002, then T037.

### Within Each User Story

- Tests are written first and fail before implementation.
- Application code comes before the scripts and infrastructure that run it, and those come
  before the developer action that validates the story.
- A story is complete when its tests pass and its developer action's checks are recorded.

### Parallel Opportunities

- **Setup**: T001 alongside T002; T003 at any time.
- **Foundational tests**: T004, T005, and T006 in parallel. T010 and T011 alongside T007 to T009.
- **US1 tests**: T012 to T018 in parallel.
- **US1 implementation**: T019, T020, and T021 in parallel; T022 after T021; T023 and T024 after
  T022; T025 independent; T026, T027, T029, and T030 in parallel with the backend work; T028
  after T027; T031 after T028 and T029; T032 after T029 and T031; T033 to T035 in sequence with
  each other but parallel to the application work.
- **US2 tests**: T039 to T044 in parallel. T050, T051, and T055 alongside T045 to T049.
- **US4**: T065 and T066 in parallel; T067 and T069 in parallel.

---

## Parallel Example: User Story 1 tests

```bash
Task: "Unit tests for production settings in backend/tests/unit/test_production_settings.py"
Task: "Unit tests for the rate limiter in backend/tests/unit/test_ratelimit.py"
Task: "Unit tests for looking up a user by login in backend/tests/unit/test_github_users_client.py"
Task: "Integration tests for the access decision in backend/tests/integration/test_access_list.py"
Task: "Integration tests for the operator commands in backend/tests/integration/test_ops_commands.py"
Task: "Integration tests for pilot-wide limits in backend/tests/integration/test_pilot_limits.py"
Task: "Playwright tests in frontend/tests/e2e/pilot-access.spec.ts"
```

## Parallel Example: User Story 1 deployment files

```bash
Task: "Create deploy/Caddyfile"
Task: "Create deploy/render-config.sh"
Task: "Create deploy/put-secrets.sh"
Task: "Frontend messages in frontend/src/app/page.tsx and frontend/src/lib/api/"
```

---

## Implementation Strategy

### MVP First (User Story 1)

1. Complete Phase 1 (Setup) and Phase 2 (Foundational).
2. Complete User Story 1: the access list, limits, rate limiting, production validation, the
   Compose and Caddy files, the scripts for releasing by hand and their tests, and the Terraform
   modules.
3. **Stop and validate** with T038: the pilot is live for invited users.

User Story 1 is the smallest useful release: invited users can use CodeAtlas on their own
repositories at a public address, with spend and access under control.

### Incremental Delivery

1. Foundation, then User Story 1: the live pilot.
2. Add User Story 2: alerts, logs, metrics, backups, and the recovery exercise.
3. Add User Story 3: approved, checked, and reversible releases from CI.
4. Add User Story 4: the load test, the evaluation, and the README. Start the benchmark early.
5. Polish: log hygiene, documentation links, scans, and final validation; the 14-day and
   one-month checks follow.

---

## Notes

- [P] tasks touch different files and have no dependency on an incomplete task.
- The [Story] label maps each task to its user story for traceability.
- Commit after each task or logical group, in Conventional Commits format with the task ID, for
  example `feat(auth): refuse sign-in outside the pilot access list (T023)`.
- tasks.md is committed with the spec, like the earlier task lists.
- No product code writes to AWS or GitHub. Terraform applies, releases, parameters, DNS, the GitHub
  App and environment settings, operator commands on the pilot, and the scans are developer
  actions: an agent may draft the commands, and runs them only after the developer confirms that
  draft.
