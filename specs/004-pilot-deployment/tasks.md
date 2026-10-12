---

description: "Task list for Pilot Deployment on Railway"
---

# Tasks: Pilot Deployment

**Input**: Design documents from `specs/004-pilot-deployment/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/http-api.md,
contracts/operations.md, quickstart.md

**Tests**: Included. The project's tested-core rule requires automated tests for core logic, and
a story is not done until its tests pass. Within each story, write the tests first and confirm
that they fail before implementing. The platform's behavior (deployments, healthchecks, the
monitors) is validated by the recorded exercises in quickstart.md.

**Earlier work**: this list replaces the task list for one AWS host, which is in the history at
`6c16f34`. That list completed everything except its developer actions and two documentation
tasks. The application work it produced is kept and not repeated here (plan.md, "Carried over");
its open tasks reappear below in their Railway form.

**Organization**: Tasks are grouped by user story, so each story can be implemented and tested as
an increment.

**Developer actions**: Tasks marked "Developer action" write to an external system: Railway
(configuration, variables, environments, domains, deployments), GitHub settings, Better Stack, the
pilot's data, or `main`, where every merge releases. An agent may prepare the exact commands and
summaries, and runs nothing until the developer confirms that draft; any change to the draft needs
a new confirmation. Reading (`railway config plan`, `railway status`, `railway logs`,
`railway metrics`) needs no confirmation.

**Test settings**: tests that need settings other than those in backend/tests/conftest.py change
the cached settings through the existing `settings` fixture (`monkeypatch.setattr(settings, ...)`),
never by changing environment variables, because `get_settings` is cached and a changed value would
leak into later tests (research R18). Tests of settings parsing itself build
`Settings(_env_file=None)` with `monkeypatch.setenv` and `monkeypatch.delenv`.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: The user story the task belongs to (US1 to US4)
- Paths are relative to the repository root: `backend/src/codeatlas/`, `backend/tests/`,
  `frontend/`, `.railway/`, `.github/workflows/`, `docs/`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Remove the AWS deployment, prepare the backend image and CI, and open the accounts

- [ ] T001 Remove the AWS deployment (research R20): delete the `infra/` and `deploy/` directories
  entirely (including `infra/**/.terraform.lock.hcl` and `deploy/tests/`) and
  `.github/workflows/release.yml`; delete the `infra` job from `.github/workflows/ci.yml`; delete
  the Terraform entries from `.gitignore` (`.terraform/`, `*.tfstate`, `*.tfstate.*`, `*.tfplan`,
  `plan.out`, `infra/**/*.tfvars`, `infra/**/backend.hcl`) and keep every other rule.
- [ ] T002 [P] Backend image and dependency (research R2, R12): run `uv add boto3` in `backend/`
  (updating backend/pyproject.toml and backend/uv.lock). In backend/Dockerfile, before the
  dependency layers, install Debian's `postgresql-client-17` with
  `apt-get install --no-install-recommends` and remove the apt lists in the same layer; replace the
  comment "A fixed UID: render-config gives the containers' secret files to UID 1000." with one
  that says the app runs as a fixed non-root UID; keep the `CODEATLAS_RELEASE` build argument for
  local builds. Check that `docker build backend` succeeds and that `pg_dump --version` in the image
  prints 17.
- [ ] T003 Backup tools in CI (research R18; after T001, same file): in `.github/workflows/ci.yml`'s
  `backend` job, before `uv run pytest`, install `postgresql-client-17` from the PostgreSQL apt
  repository (`apt.postgresql.org`, for the runner's Ubuntu codename) and add
  `/usr/lib/postgresql/17/bin` to `$GITHUB_PATH`, so the backup tests run instead of skipping.
- [ ] T004 Developer action (needed before T023): accounts and tools (quickstart.md
  "Prerequisites"): a Railway account on the Hobby plan; the Railway CLI with `railway config` and
  `railway ssh`, signed in with `railway login`; the developer's SSH public key registered on the
  Railway account; a Better Stack account on the free plan.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: What every service needs to start on Railway: the new settings, the private key as a
variable, the release from the platform, migrations in the pre-deploy command, and the worker's
schema wait

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

### Tests for the foundation ⚠️ (write first, confirm they fail)

- [ ] T005 [P] Unit tests in backend/tests/unit/test_config.py for the new settings (data-model.md,
  "Settings"), read with `Settings(_env_file=None)` and a clean environment:
  - defaults: `github_app_private_key` is `""`; `client_address_header` is `""`;
    `worker_report_seconds` 60; `worker_silent_seconds` 300; `jobs_waiting_alert_seconds` 1800;
    `jobs_failing_alert_count` 3; `backup_missing_hours` 26; `backup_retention_days` 7;
    `backup_endpoint_url`, `backup_region`, `backup_bucket`, `backup_access_key_id`, and
    `backup_secret_access_key` are `""`;
  - `release` is `CODEATLAS_RELEASE` when set, else `RAILWAY_GIT_COMMIT_SHA`, else
    `"development"`; with both set, `CODEATLAS_RELEASE` wins.
- [ ] T006 [P] Unit tests in backend/tests/unit/test_production_settings.py for the private key rule
  (research R5): production settings pass with `GITHUB_APP_PRIVATE_KEY` set to a PEM and
  `GITHUB_APP_PRIVATE_KEY_PATH` unset; they pass with a readable key file and no content; with
  neither, the error names `GITHUB_APP_PRIVATE_KEY` and contains neither the PEM nor the path.
  Update the existing tests that expect `GITHUB_APP_PRIVATE_KEY_PATH` in the error.
- [ ] T007 [P] Unit tests in backend/tests/unit/test_github_client_auth.py for signing the App's JWT
  with the key content: generate an RSA key with `cryptography`; with `github_app_private_key` set,
  the token `GitHubClient._app_jwt()` returns verifies with the public key and has `iss` equal to
  `github_app_id`; with only a key file, the same; with both, the content is used (the file holds a
  different key); with neither, the error names both `GITHUB_APP_PRIVATE_KEY` and
  `GITHUB_APP_PRIVATE_KEY_PATH`.
- [ ] T008 [P] Unit tests in backend/tests/unit/test_migrate_decision.py for the pure functions of
  `codeatlas.ops.migrate` (research R7), with plain revision sets:
  - `decide(database_revision, known_revisions, head)` returns `"upgrade"` when the database's
    revision is `None` or known and not `head`, `"current"` when it equals `head`, and `"ahead"`
    when it is not in `known_revisions`;
  - `schema_ready(database_revision, known_revisions, head)` is true when the revision equals
    `head` or is unknown (ahead), and false when it is `None` or known and older.
- [ ] T009 [P] Integration tests in backend/tests/integration/test_ops_migrate.py, each on a scratch
  database created and dropped by the test (as backend/tests/conftest.py creates the test
  database):
  - `main(["migrate"])` on an empty database upgrades to the head revision and exits 0; a second
    run changes nothing and exits 0;
  - with `alembic_version` set to `ffffffffffff`, it exits 0, logs `migrations_skipped` with
    `database_revision` and `release_revision`, and changes no table;
  - when `alembic.command.upgrade` raises (patched), it exits 1;
  - `wait_for_schema(timeout=..., interval=...)` returns true at once on a current or ahead
    database, and returns false after the timeout on a database stamped at `0003`.
- [ ] T010 [P] Extend backend/tests/unit/test_logging_redaction.py: the new structured fields
  `queued_jobs`, `oldest_runnable_job_age_seconds`, `database_revision`, `release_revision`,
  `object_key`, `bytes`, and `pruned` reach the top level of the JSON line, and a key outside the
  allow-list is still dropped.

### Implementation for the foundation

- [ ] T011 Settings in backend/src/codeatlas/config.py (data-model.md, "Settings"):
  - add `github_app_private_key: str = ""`, `client_address_header: str = ""`,
    `worker_report_seconds: int = 60`, `worker_silent_seconds: int = 300`,
    `jobs_waiting_alert_seconds: int = 1800`, `jobs_failing_alert_count: int = 3`,
    `backup_missing_hours: int = 26`, `backup_endpoint_url`, `backup_region`, `backup_bucket`,
    `backup_access_key_id`, and `backup_secret_access_key` (all `str = ""`), and
    `backup_retention_days: int = 7`;
  - read `release` with `validation_alias=AliasChoices("CODEATLAS_RELEASE",
    "RAILWAY_GIT_COMMIT_SHA")`, and change its comment to say the platform provides the commit
    (research R6);
  - in `_complete_in_production`, replace the `GITHUB_APP_PRIVATE_KEY_PATH` entry with
    `"GITHUB_APP_PRIVATE_KEY": bool(self.github_app_private_key.strip()) or
    _readable_file(self.github_app_private_key_path)`;
  - keep `emit_metrics` and `metrics_environment` until T034 removes them with their only users;
  - in .env.example, add `CLIENT_ADDRESS_HEADER=` with a comment that the platform sets it to
    `X-Real-IP` on `api` and that development leaves it empty.
- [ ] T012 [P] Sign with the key content in backend/src/codeatlas/github/client.py: `_app_jwt` uses
  `github_app_private_key` when it is not blank and otherwise reads
  `github_app_private_key_path`; with neither, it raises `RuntimeError` naming both settings.
- [ ] T013 [P] Add `queued_jobs`, `oldest_runnable_job_age_seconds`, `database_revision`,
  `release_revision`, `object_key`, `bytes`, and `pruned` to `STRUCTURED_FIELDS` in
  backend/src/codeatlas/logging.py.
- [ ] T014 Create backend/src/codeatlas/ops/migrate.py (research R7; contracts/operations.md,
  "Operator commands"):
  - pure `decide` and `schema_ready` as T008 describes;
  - `known_revisions_and_head()` from Alembic's `ScriptDirectory`, with the `Config` built from
    `alembic.ini` found at `Path(__file__).resolve().parents[3]` (`backend/` in the repository,
    `/app` in the image) and its `script_location` set to the `alembic` directory beside it;
  - `database_revision(connection)`: `alembic_version.version_num`, or `None` when the table does
    not exist;
  - `run_migrations()`: on one connection, take `pg_advisory_lock` with a fixed key, decide, run
    `command.upgrade(config, "head")` for `"upgrade"`, log `migrations_skipped` with
    `database_revision` and `release_revision` for `"ahead"`, and release the lock in `finally`;
  - `wait_for_schema(timeout=timedelta(minutes=10), interval=timedelta(seconds=5))`: polls
    `schema_ready` and returns whether it became ready, logging once while it waits.
- [ ] T015 Add the `migrate` command to backend/src/codeatlas/ops/__main__.py: it runs
  `run_migrations()` outside the per-command session and exits 0 when migrated, current, or
  skipped, and 1 with a one-line explanation when a migration fails. Rewrite the module docstring
  for the platform: commands run as `railway ssh -s api -- python -m codeatlas.ops <command>`, and
  `migrate` is `api`'s pre-deploy command.
- [ ] T016 Wait for the schema in backend/src/codeatlas/jobs/worker.py: in `main()`, after
  `configure_logging()` and before the job loop, call `wait_for_schema()`; when it returns false,
  log an error naming the database's and the release's revisions and exit with status 1, so the
  restart policy tries again (research R7).

**Checkpoint**: the backend starts with a key held in a variable, reports the platform's commit,
migrates through `ops migrate`, and the worker never runs against an older schema.

---

## Phase 3: User Story 1 - Use CodeAtlas at a public address (Priority: P1) 🎯 MVP

**Goal**: Invited users use every feature at the web app's Railway domain over HTTPS; visitors are
turned away; only the web app is public.

**Independent Test**: Following the operations guide, the developer applies the configuration,
sets the secrets, and deploys `main`. A pilot user signs in, connects a repository, sees a push
indexed, asks a question, and reviews a pull request; a second account is turned away
(quickstart.md scenarios 1 to 11 and 20).

### Tests for User Story 1 ⚠️ (write first, confirm they fail)

- [ ] T017 [P] [US1] Unit tests in backend/tests/unit/test_client_address.py for the rate limit's
  client address (research R4), each building its own app with `create_app()` after setting
  `rate_limit_per_minute` to 2 through the `settings` fixture:
  - with `client_address_header` set to `X-Real-IP`, requests to `/auth/github/login` with two
    different `X-Real-IP` values are counted separately, and three with the same value get 429 on
    the third;
  - with the header configured but absent from the request, the connection's address is counted;
  - with no header configured, `X-Real-IP` is ignored and all requests share the connection's
    window.

### Implementation for User Story 1

- [ ] T018 [US1] Client address in backend/src/codeatlas/api/app.py: a helper
  `_client_address(request, header)` returns the configured header's value when the setting is set
  and the header is present and not blank, and otherwise `request.client.host` (or `"unknown"`);
  the `rate_limit` middleware uses it. Replace the comment about Caddy and `X-Forwarded-For` with
  one citing research R4 (`X-Real-IP` is set and overwritten by the platform's edge, and the API
  has no public address).
- [ ] T019 [P] [US1] Routing in frontend/next.config.ts (research R3; contracts/http-api.md, "Public
  routing"): add rewrites for `/webhooks/:path*`, `/status/:path*`, `/readyz`, and `/version` to
  `${apiOrigin}` next to `/v1` and `/auth`; add `headers()` returning
  `Strict-Transport-Security: max-age=31536000` for `/:path*`; rewrite the top comment: in hosted
  environments the web app forwards these paths to the API over the private network, and
  `API_ORIGIN` is fixed when Next.js builds.
- [ ] T020 [P] [US1] Build arguments in frontend/Dockerfile's `build` stage (research R3, R6):
  declare `ARG API_ORIGIN=http://localhost:8000` and set `ENV API_ORIGIN=$API_ORIGIN` before
  `npm run build`; declare `ARG RAILWAY_GIT_COMMIT_SHA=` next to `NEXT_PUBLIC_RELEASE`, and run the
  build with `NEXT_PUBLIC_RELEASE="${RAILWAY_GIT_COMMIT_SHA:-$NEXT_PUBLIC_RELEASE}"`. Check with
  `docker build --target production --build-arg API_ORIGIN=http://api.example:8000 frontend` that
  `.next/routes-manifest.json` in the build stage holds that origin.
- [ ] T021 [US1] Create .railway/railway.ts with Railway's TypeScript infrastructure as code
  (research R8; contracts/operations.md, "Services" and "Variables"), following Railway's reference
  for its exact API:
  - the project `codeatlas` and the environment `production`, in the US East region;
  - `web` (`main`, root `frontend`, its Dockerfile, healthcheck `/`), `api` (`main`, root
    `backend`, start command `uvicorn codeatlas.api.app:app --host 0.0.0.0 --port 8000`,
    pre-deploy `python -m codeatlas.ops migrate`, healthcheck `/readyz`), `worker` (`main`, root
    `backend`, start command `python -m codeatlas.jobs.worker`, no healthcheck), each with restart
    policy on failure and 10 retries and one replica;
  - `db` from the image `pgvector/pgvector:pg17` with a volume at `/var/lib/postgresql/data`,
    `PGDATA=/var/lib/postgresql/data/pgdata`, start command
    `postgres -c shared_buffers=256MB -c max_connections=50`, no TCP proxy;
  - every variable in contracts/operations.md "Variables" for these services, with reference
    variables for `APP_ORIGIN`, `DATABASE_URL`, and `API_ORIGIN`, and every secret declared with
    `preserve()` so that no value is in the file;
  - the size limits of "Services" if the package's types accept them;
  - no backup service, bucket, or deployment timings yet (T039, T043).
  Check it with `railway config plan` (reading only) and record anything the plan does not show in
  the table of T022.
- [ ] T022 [US1] Rewrite docs/operations.md for Railway, part 1 (FR-030; research R17): replace the
  whole AWS guide with "Conventions" (developer actions and what needs no confirmation),
  "Environments", "One-time setup" (tools; the project from `.railway/railway.ts` with
  `railway config plan` and `apply`; generating the web app's domain; the pilot GitHub App with its
  callback and webhook URLs; setting each sealed secret on the services that use it; the first
  deployment), "Settings outside the infrastructure file" (the table in contracts/operations.md
  with its values), "Managing pilot users" and "Deleting a removed user's data" (through
  `railway ssh -s api`), "Temporary settings" (a variable change, its staged diff, and "Deploy"),
  "Replacing a credential" (each one; `POSTGRES_PASSWORD` with `ALTER ROLE` through
  `railway ssh -s db` first), and "Tearing down".
- [ ] T023 [US1] Developer action: build the pilot and validate User Story 1 (needs T004 and every
  earlier task merged to `main`): apply `.railway/railway.ts`; generate the domain; register the
  pilot GitHub App; set the secrets; let `main` deploy; add the pilot users. Run quickstart.md
  scenarios 1 to 11 and 20, and record the results and dates in its "Validation record".

**Checkpoint**: the pilot is live for invited users at its Railway address.

---

## Phase 4: User Story 2 - Learn about failures and recover from them (Priority: P2)

**Goal**: The developer is emailed when the pilot is unreachable, the worker stops, jobs pile up or
keep failing, a backup is missing, a release fails, or spending passes $20; logs and metrics are
searchable; nightly backups exist and a recovery has been rehearsed.

**Independent Test**: Stop the worker with jobs queued and receive the alert; stop the web app and
receive the unreachability alert; duplicate `production` into `drill`, restore the latest backup,
and see the pilot user's data, recording the time (quickstart.md fault tests and recovery
exercise).

### Tests for User Story 2 ⚠️ (write first, confirm they fail)

- [ ] T024 [P] [US2] Extend backend/tests/integration/test_pilot_schema.py for migration `0005`
  (data-model.md):
  - `worker_heartbeats`: `worker_id` text primary key, `started_at` and `beat_at` timestamptz not
    null;
  - `backups`: `id` uuid primary key; `object_key` text, "Unique"; `completed_at` timestamptz;
    `bytes` bigint; `sha256` text, "64 lowercase hexadecimal characters" (a check constraint
    rejects `ABC` and a 63-character value); `alembic_revision` and `release` text, all not null;
  - `jobs.error_permanent` is boolean and "nullable";
  - `alembic_version.version_num` is `0005` (replacing the `0004` assertion).
- [ ] T025 [P] [US2] Unit tests in backend/tests/unit/test_status_conditions.py for the pure
  condition functions of `codeatlas.status` (data-model.md, "Alert conditions"), each returning the
  condition's name, whether it fails, its value, and its threshold:
  - `worker`: fails when the newest heartbeat is older than `WORKER_SILENT_SECONDS` (300 s ok,
    301 s failing) or there is none (value `None`);
  - `jobs-waiting`: fails when the oldest runnable job became due longer ago than the threshold
    (1,800 s ok, 1,801 s failing); value 0 when there is none;
  - `jobs-failing`: fails when the count is "At least the threshold" (2 ok, 3 failing);
  - `backup`: fails when the newest backup is older than `BACKUP_MISSING_HOURS` (26 h ok, 26 h and
    1 s failing) or there is none (value `None`).
- [ ] T026 [P] [US2] Integration tests in backend/tests/integration/test_status_endpoints.py
  (contracts/http-api.md, `GET /status/{condition}`):
  - `worker`: a heartbeat 10 s old gives 200 with its value; 301 s old gives 503; none gives 503
    with `value` null;
  - `jobs-waiting`: no job gives 200 with value 0; a job due 10 minutes ago gives 200; one due 31
    minutes ago gives 503; one due 31 minutes ago behind a running job of the same workspace and
    kind gives 200;
  - `jobs-failing`: 3 jobs failed in the last hour with counted failures (one with
    `error_permanent` false, two with code `timeout` and `error_permanent` true) give 503; 2 give
    200; 3 permanent failures with code `limit_exceeded` give 200; 3 counted failures finished 61
    minutes ago give 200;
  - `backup`: no row gives 503 with `value` null; a backup 2 hours old gives 200; 27 hours old gives
    503;
  - an unknown condition gives 404 `not_found`; with the session factory patched to raise
    `OperationalError` (as test_readiness.py does), each gives 503 `not_ready`;
  - every response has `Cache-Control: no-store`; no status path is in the OpenAPI document;
  - with `rate_limit_per_minute` 2 in an app built with `create_app()`, the third request to a
    status path gets 429 `rate_limited`.
- [ ] T027 [P] [US2] Integration tests in backend/tests/integration/test_worker_reporter.py,
  replacing backend/tests/integration/test_worker_metrics.py (delete it): `status.report_once()`
  inserts the worker's `worker_heartbeats` row with `started_at` and `beat_at`, and a second call
  moves only `beat_at`; a row whose `beat_at` is 25 hours old is deleted; a row 23 hours old
  stays; it logs one `worker_status` line with `queued_jobs` (jobs `queued` or `retry_wait`) and
  `oldest_runnable_job_age_seconds` (by the `runnable_waiting` rule, 0 when none); and the worker's
  `main()` starts the reporter with no setting enabled (job loop patched to stop at once).
- [ ] T028 [P] [US2] Extend backend/tests/integration/test_job_queue.py: a job failed through
  `fail` with a non-permanent failure on its last attempt has `error_permanent` false and its code;
  a permanent failure sets it true; a job failed at claim time for its deadline has code `timeout`
  and `error_permanent` true; nothing calls the removed metrics module.
- [ ] T029 [P] [US2] Unit tests in backend/tests/unit/test_backup_pruning.py for
  `codeatlas.ops.backup.expired_keys(keys, today, retention_days)`: with `today` 2026-10-20 and 7
  days, keys under `backups/2026-10-12/` are expired and keys under `backups/2026-10-13/` to
  `backups/2026-10-20/` are kept; keys whose second path segment is not a date are never expired.
- [ ] T030 [P] [US2] Integration tests in backend/tests/integration/test_backup_restore.py, skipped
  with a reason unless `pg_dump --version` reports 17 or later, using an in-memory object store
  (a test helper implementing the storage protocol of T036):
  - `run_backup()` stores `backups/<UTC date>/codeatlas.dump` and then
    `backups/<UTC date>/manifest.json`, and writes one `backups` row whose `object_key`, `bytes`,
    and `sha256` match the stored dump, with `alembic_revision` `0005` and the release; the
    manifest's row counts equal the database's tables;
  - a stored backup dated 9 days ago is deleted with its row; one dated 3 days ago stays;
  - when storing the manifest fails, no row is written and the command exits 1;
  - `run_restore()` of the stored backup into a second scratch database makes `verify-restore`
    pass; a dump whose bytes no longer match the manifest's SHA-256 is refused before
    `pg_restore` runs.
- [ ] T031 [P] [US2] Update backend/tests/integration/test_ops_commands.py: remove the
  `backup-manifest` tests; keep the `verify-restore` tests; add that `backup` and `restore` exit 1
  naming each missing `BACKUP_*` setting without printing any value, and that `restore` without
  `--yes` restores nothing when the answer is not `yes`.

### Implementation for User Story 2

- [ ] T032 [US2] Migration backend/alembic/versions/0005_operations_status.py with
  `revision = "0005"` and `down_revision = "0004"`, and models in backend/src/codeatlas/models.py
  (data-model.md):
  - `WorkerHeartbeat` (`worker_heartbeats`): `worker_id` Text primary key, `started_at` and
    `beat_at` not null;
  - `Backup` (`backups`): `id` UUID primary key (default `uuid4`), `object_key` Text unique,
    `completed_at`, `bytes` BigInteger, `sha256` Text with check `sha256 ~ '^[0-9a-f]{64}$'`,
    `alembic_revision` Text, `release` Text, all not null;
  - `Job.error_permanent`: Boolean, nullable;
  - the downgrade drops the column and both tables.
- [ ] T033 [US2] Record permanence in backend/src/codeatlas/jobs/queue.py: `_mark_failed` sets
  `job.error_permanent = failure.permanent`; remove the `metrics.emit` call and the import; add
  `counted_failure()`, a SQL predicate (`error_permanent IS false OR error_code IN
  COUNTED_PERMANENT_CODES`), and update the comment on `COUNTED_PERMANENT_CODES` to say the
  failing-jobs check reads it (research R10).
- [ ] T034 [US2] Create backend/src/codeatlas/status.py, replacing backend/src/codeatlas/metrics.py
  (delete it and backend/tests/unit/test_metrics.py):
  - the pure condition functions of T025;
  - readers for each condition over a session at a given time: the newest `beat_at`; the oldest
    `run_after` under `queue.runnable_waiting`; the count of jobs with `status = 'failed'`,
    `finished_at` within the last hour, and `counted_failure()`; the newest `backups.completed_at`;
  - `report_once()`: upserts the process's row (`<hostname>:<pid>`, `started_at` fixed at process
    start) with `INSERT ... ON CONFLICT`, deletes rows with `beat_at` more than 24 hours old in the
    same transaction, and logs `worker_status` with `queued_jobs` and
    `oldest_runnable_job_age_seconds`;
  - `start_reporter(interval)`, a daemon thread as before.
  In backend/src/codeatlas/jobs/worker.py, start the reporter always, with
  `get_settings().worker_report_seconds`. Remove `emit_metrics` and `metrics_environment` from
  backend/src/codeatlas/config.py, and add to backend/tests/unit/test_config.py that an
  `EMIT_METRICS` variable is ignored.
- [ ] T035 [US2] Create backend/src/codeatlas/api/routes/status.py with `GET /status/{condition}`
  (`include_in_schema=False`) per contracts/http-api.md: 200 or 503 with `condition`, `status`
  (`ok` or `failing`), `value`, and `threshold`; 404 `not_found` for another name; 503 `not_ready`
  when the database raises `SQLAlchemyError`; `Cache-Control: no-store` on every response. Include
  the router in backend/src/codeatlas/api/app.py and add `"/status/"` to
  `RATE_LIMITED_PATH_PREFIXES`.
- [ ] T036 [P] [US2] Create backend/src/codeatlas/ops/storage.py (research R12): an `ObjectStore`
  protocol (`put(key, path)`, `get(key, path)`, `list(prefix) -> list[str]`,
  `delete(keys)`) and `S3Store`, a `boto3` client built from the `BACKUP_*` settings with
  `endpoint_url`, `region_name`, the key pair, and virtual-hosted addressing, which Railway's
  current buckets use. `S3Store.from_settings()` raises one error naming every missing `BACKUP_*`
  setting, with no value.
- [ ] T037 [P] [US2] Create backend/src/codeatlas/ops/dump.py (research R12):
  - the libpq environment (`PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`) derived from
    `DATABASE_URL`, so no password appears on a command line;
  - `snapshot_dump(path)`: on one connection in `REPEATABLE READ`, `SELECT pg_export_snapshot()`,
    then `pg_dump -Fc --snapshot=<id> --file=<path>` while the transaction stays open, then the row
    count of every table in the `public` schema and the Alembic revision in the same transaction;
    returns the counts document that `backup.build_manifest` reads;
  - `check_dump(path)` (`pg_restore --list`), `restore_dump(path)`
    (`pg_restore --clean --if-exists --no-owner --exit-on-error`), and `sha256_of(path)`.
- [ ] T038 [US2] Backup and restore commands (contracts/operations.md, "Operator commands"):
  - add `expired_keys` to backend/src/codeatlas/ops/backup.py;
  - in backend/src/codeatlas/ops/__main__.py, add `backup` (snapshot dump, check, manifest, upload
    of the dump and then the manifest, the `backups` row, then pruning of expired keys and their
    rows; logs `backup_completed` with `object_key` and `bytes`, and `backup_pruned` with
    `pruned`; exits 1 with no row on any failure) and `restore <UTC date> [--yes]` (download to a
    temporary directory, SHA-256 check against the manifest, confirmation unless `--yes`,
    `restore_dump`, then the `verify-restore` comparison); remove `backup-manifest`;
  - both use only `DATABASE_URL`, the release, and the `BACKUP_*` settings, so they run on a service
    without `CODEATLAS_ENV` and the application's secrets.
- [ ] T039 [US2] Add to .railway/railway.ts (after T021): the `backups` bucket; the `backup`
  service (`main`, root `backend`, start command `python -m codeatlas.ops backup`, cron
  `30 3 * * *`, restart policy never, its size limit) with `DATABASE_URL`, the `BACKUP_*` reference
  variables to the bucket, and `BACKUP_RETENTION_DAYS=7`, and no other variable. Check with
  `railway config plan`; if the plan does not show the cron schedule, add it to the operations
  guide's table of settings outside the file.
- [ ] T040 [US2] docs/operations.md, part 2 (after T022): "Alerts" (Railway's notification rules
  for build failed, deployment failed, and deployment crashed; usage limits with the $20 email alert
  and the $50 hard limit, and restarting services after the hard limit; the six Better Stack
  monitors from contracts/operations.md with their URLs, 3-minute interval, and confirmation, the
  backup monitor created after the first backup), "Logs" (the filters), "Metrics" (the service
  views and `railway metrics --http`), "Backups" (schedule, listing the bucket, running one by
  hand), "Restoring a backup" (`railway down -s worker`, the bucket's credentials exported in a
  `railway ssh -s api` session, `ops restore`, redeploying the worker, and the backup alert to
  expect), "Recovery exercise", "Losing the database volume", "Losing the environment or project",
  and "Fault tests" (the quickstart.md table).
- [ ] T041 [US2] Developer action: set up alerting and validate User Story 2 (needs T023 and every
  US2 task merged): set the notification rules and usage limits; create the monitors (the backup
  monitor after the first nightly backup); run the fault tests (SC-007), the recovery exercise
  (SC-008), and quickstart.md scenarios 18 and 21; record the times and results.

**Checkpoint**: failures reach the developer by email, and the pilot can be rebuilt from its
configuration and a backup.

---

## Phase 5: User Story 3 - Release changes safely (Priority: P3)

**Goal**: Every merge into `main` is released once CI passes, migrations first, with traffic moved
only after the readiness check, schema changes flagged in review, and one-action rollback per
service.

**Independent Test**: Merge a visible change and see it serve within 15 minutes; release a version
whose readiness check fails and see the previous one keep serving, with an email; roll back and see
it serve within 5 minutes (quickstart.md scenarios 12 to 17 and 19).

### Tests for User Story 3

The release logic in the application, the migration decision, skipping when the database is ahead,
and the worker's schema wait, is tested in Phase 2 (T008, T009). The rest is platform behavior,
validated in T045.

### Implementation for User Story 3

- [ ] T042 [US3] Schema report in .github/workflows/ci.yml (after T003): a `schema` job that runs
  only on `pull_request`, checks out with `fetch-depth: 0`, passes the base branch through an `env`
  variable (not an inline expression in `run`), lists the files the pull request adds or modifies
  under `backend/alembic/versions/` with `git diff --name-only --diff-filter=AM
  "origin/$BASE_REF...HEAD"`, and when there are any, writes them to `$GITHUB_STEP_SUMMARY` with the
  expand-then-contract rule (add in one release, remove in a later one, so the running version keeps
  working) and emits one `::warning title=Schema change::` annotation; otherwise it writes "No
  schema change". It never fails.
- [ ] T043 [US3] Add to .railway/railway.ts (after T039): `RAILWAY_DEPLOYMENT_OVERLAP_SECONDS=10`
  and `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=10` on `web` and `api`,
  `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=30` on `worker`, and "Wait for CI" on `web`, `api`,
  `worker`, and `backup` (the source's check suites, if the package's types accept it). Check with
  `railway config plan`; if "Wait for CI" is not in the plan, add it to the operations guide's table
  of settings outside the file.
- [ ] T044 [US3] docs/operations.md, part 3 (after T040): "Releases" (merge through pull requests,
  because a direct push to `main` also releases; what Railway does and how to watch it),
  "Migrations: expand, then contract" (with the `schema` job), "Rolling back" (each service's
  "Rollback", the 72-hour image window on Hobby, redeploying an older commit after it, and the
  API's skipped migrations), "Rehearsing a failed release" (the healthcheck path), and "Changing the
  configuration" (`railway config plan` and `apply`, and staged changes in the dashboard).
- [ ] T045 [US3] Developer action: validate User Story 3: quickstart.md scenarios 12 to 17 and 19;
  at least five releases with the probe (SC-009); the failed-readiness rehearsal and rollbacks
  (SC-010); record the results.

**Checkpoint**: releases follow merges safely and can be reversed.

---

## Phase 6: User Story 4 - Publish measured results (Priority: P4)

**Goal**: The README shows a demo, the deployed architecture, the known limitations, and links to a
load test report and an evaluation report that state how they were measured.

**Independent Test**: Rerun the load test from its report's steps on its recorded commit and get a
report of the same form; every number in both reports has its denominator, commit, and
configuration.

### Tests for User Story 4

The load test and evaluation runners keep their existing tests (`test_perf_check.py`,
`test_bench_eval.py`, and the evaluation metric tests).

### Implementation for User Story 4

- [ ] T046 [P] [US4] Load test against Railway (research R15): in .github/workflows/load-test.yml,
  measure the network baseline against `$BASE_URL/version` instead of `/healthz` (the step, its
  comment, and the summary text); in the docstring of backend/evals/perf_check.py, describe the
  hosted target as a `loadtest` environment duplicated from `production` with the variables of
  research R15.
- [ ] T047 [US4] Developer action: run the load test (SC-011; needs T023 and T046): duplicate
  `production` into `loadtest`, set its variables (research R15), remove its backup schedule, run
  `load-test.yml` against its domain, read each service's CPU and memory from Railway's metrics,
  delete the environment, and write docs/reports/load-test.md with the steps to rerun it, the
  service sizes, the configuration, the commit, each level's results, and the highest level that
  meets every target.
- [ ] T048 [P] [US4] Developer action: run the evaluation (SC-012; independent of hosting, so start
  it early, before the published judges are retired, research R16): the benchmark, question, and
  review runs in quickstart.md "Evaluation", then write docs/reports/evaluation.md with
  denominators, per-item failures, model cost and latency per item, the commit, set versions and
  SHA-256s, the review status of each set, and the caveats.
- [ ] T049 [US4] Rewrite README.md's AWS content for Railway (FR-029, FR-031; research R17):
  "Deployment" (the Railway services, the public address, releases on merge, monitoring, and a
  link to the operations guide); "Architecture" with a Mermaid diagram (browser and GitHub to the
  platform's edge, `web`, the private network to `api`, `worker` and `api` to `db`, `backup` to the
  bucket, the model providers, and the external monitor); "Known limitations" with every item of
  FR-031; "Data retention" with the 10-day backup sentence; "Measured results" and "Documentation"
  linking both reports and the operations guide.
- [ ] T050 [US4] Developer action: record the demo GIF from the pilot (at most 60 seconds, under 10
  MB: connect a repository, ask where something is implemented, open the cited code, review a pull
  request), save it as docs/assets/demo.gif, and show it at the top of README.md.

**Checkpoint**: the README presents the running pilot and its measured results.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T051 [P] Remove stale references to the AWS design outside specs/ and docs/decisions/: update
  the fourth-increment note in docs/design/codeatlas-v1.md (ADRs 0013 to 0016 apply: the pilot runs
  on Railway, without ECS, RDS, SQS, or a staging environment); then search the repository for
  `Caddy`, `CloudWatch`, `Terraform`, `render-config`, `codeatlas-compose`, `Session Manager`,
  `SSM`, `infra/`, and `deploy/`, and fix every remaining mention in code comments, docstrings,
  README.md, and docs/.
- [ ] T052 Run every command in quickstart.md "Automated checks" and fix any failure.
- [ ] T053 Developer action: credential scan (SC-006): `gitleaks git` over the repository history;
  an export of 7 days of logs (`railway logs` for each service) scanned with `gitleaks dir`; and a
  check that the Dockerfiles declare only the build arguments `API_ORIGIN`,
  `RAILWAY_GIT_COMMIT_SHA`, `NEXT_PUBLIC_RELEASE`, and `CODEATLAS_RELEASE`.
- [ ] T054 Final validation: complete quickstart.md's "Validation record" with dates and results,
  and name any scenario not run, with the reason.
- [ ] T055 Developer action, after launch: after 14 days, record the web app monitor's availability
  (SC-001); after the first full month, the month's Railway usage (SC-002); and daily model spend in
  the first month (SC-003).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies. T004 is a developer action needed by T023.
- **Foundational (Phase 2)**: depends on T002 (the `boto3` dependency and the image). It blocks
  every story.
- **User Story 1 (Phase 3)**: depends on Foundational. T023 needs every other task before it.
- **User Story 2 (Phase 4)**: its code depends only on Foundational and can be written alongside
  User Story 1; T039 and T040 follow T021 and T022 in their files, and T041 needs the running pilot
  (T023).
- **User Story 3 (Phase 5)**: depends on User Story 1; T043 follows T039, and T044 follows T040.
- **User Story 4 (Phase 6)**: T048 needs nothing from this feature and should start early; T047
  needs T023 and T046; T049 describes User Stories 1 to 3; T050 needs the running pilot.
- **Polish (Phase 7)**: depends on every story. T055 runs weeks after launch.

### Shared files (do these tasks sequentially)

- .github/workflows/ci.yml: T001, then T003, then T042.
- backend/src/codeatlas/config.py: T011, then T034.
- backend/tests/unit/test_config.py: T005, then T034.
- backend/src/codeatlas/api/app.py: T018, then T035.
- backend/src/codeatlas/ops/__main__.py: T015, then T038.
- backend/src/codeatlas/jobs/worker.py: T016, then T034.
- .railway/railway.ts: T021, then T039, then T043.
- docs/operations.md: T022, then T040, then T044.
- README.md: T049, then T050.
- quickstart.md's validation record: T023, T041, T045, then T054.

### Within Each User Story

- Tests are written first and fail before implementation.
- Application code comes before the configuration that deploys it, and both come before the
  developer action that validates the story.
- A story is complete when its tests pass and its developer action's checks are recorded.

### Parallel Opportunities

- **Setup**: T002 alongside T001; T004 at any time.
- **Foundational tests**: T005 to T010 in parallel. **Implementation**: T012 and T013 alongside
  T011; T014 after T011; T015 and T016 after T014.
- **US1**: T017, T019, and T020 in parallel; T018 after T017; T021 and T022 alongside the code.
- **US2 tests**: T024 to T031 in parallel. **Implementation**: T036 and T037 alongside T032 to
  T035; T038 after T036 and T037.
- **US4**: T046 and T048 in parallel with everything else.

---

## Parallel Example: Foundational tests

```bash
Task: "Unit tests for the new settings in backend/tests/unit/test_config.py"
Task: "Unit tests for the private key rule in backend/tests/unit/test_production_settings.py"
Task: "Unit tests for signing with the key content in backend/tests/unit/test_github_client_auth.py"
Task: "Unit tests for the migration decision in backend/tests/unit/test_migrate_decision.py"
Task: "Integration tests for ops migrate in backend/tests/integration/test_ops_migrate.py"
Task: "Structured field tests in backend/tests/unit/test_logging_redaction.py"
```

## Parallel Example: User Story 2 tests

```bash
Task: "Schema tests for 0005 in backend/tests/integration/test_pilot_schema.py"
Task: "Condition tests in backend/tests/unit/test_status_conditions.py"
Task: "Endpoint tests in backend/tests/integration/test_status_endpoints.py"
Task: "Reporter tests in backend/tests/integration/test_worker_reporter.py"
Task: "Failure permanence tests in backend/tests/integration/test_job_queue.py"
Task: "Pruning tests in backend/tests/unit/test_backup_pruning.py"
Task: "Backup and restore tests in backend/tests/integration/test_backup_restore.py"
Task: "Operator command tests in backend/tests/integration/test_ops_commands.py"
```

---

## Implementation Strategy

### MVP First (User Story 1)

1. Complete Phase 1 (Setup) and Phase 2 (Foundational).
2. Complete User Story 1: the client address, the web app's routing and build arguments, the
   infrastructure file, and the first part of the operations guide.
3. **Stop and validate** with T023: the pilot is live for invited users at its Railway address.

User Story 1 is the smallest useful release: invited users can use CodeAtlas on their own
repositories at a public address, with spend and access under control.

### Incremental Delivery

1. Foundation, then User Story 1: the live pilot.
2. Add User Story 2: status endpoints, monitors, backups, and the recovery exercise.
3. Add User Story 3: the schema report, deployment timings, "Wait for CI", and rehearsed rollbacks.
4. Add User Story 4: the load test, the evaluation, and the README. Start the evaluation early.
5. Polish: stale references, checks, the credential scan, and final validation; the 14-day and
   one-month checks follow.

---

## Notes

- [P] tasks touch different files and have no dependency on an incomplete task.
- The [Story] label maps each task to its user story for traceability.
- Commit after each task or logical group, in Conventional Commits format with the task ID, for
  example `feat(ops): skip migrations when the database is ahead of the release (T014)`.
- tasks.md is committed with the spec, like the earlier task lists.
- No product code writes to Railway, GitHub, or Better Stack. The configuration, secrets,
  environments, domains, monitors, merges into `main`, operator commands on the pilot, and the
  scans are developer actions: an agent may draft the commands, and runs them only after the
  developer confirms that draft.
