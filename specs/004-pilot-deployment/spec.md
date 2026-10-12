# Feature Specification: Pilot Deployment

**Feature Branch**: `004-pilot-deployment`

**Created**: 2026-10-07

**Status**: Draft

**Input**: User description (translated): "Build 004, deployment." In the same discussion, the
developer chose to run every service on one cloud host on AWS instead of managed container and
database services, to keep hosting costs low. On 2026-10-12 the developer replaced that choice
(translated): "I've given up deploying to AWS; deploy to Railway instead." Every service, the
database, and its backups now run on Railway, at the address it provides, with no domain name of
the developer's own. Releases go out automatically once the automated checks pass, and alerts
cover the core failure conditions.

## User Scenarios & Testing *(mandatory)*

This is the fourth increment of CodeAtlas. The first three increments (`specs/001-repository-qa`,
`specs/002-push-reindexing`, `specs/003-pr-review`) run on a developer's machine. This feature
runs CodeAtlas as a pilot service on the public internet. Invited users sign in at a public
address and use it on their own GitHub repositories. Every change merged into the main branch is
released automatically and safely, the developer learns about failures from alerts, and can
rebuild the service if its environment or its database is lost. The feature also measures the
deployed system: a load test at the pilot's service sizes, and an evaluation with the real model
providers.

A **pilot user** is someone allowed to use the pilot. A **visitor** is anyone else who signs in.
The **developer** is the person who operates CodeAtlas. The **hosting platform** is the managed
application platform that runs the pilot's services, its database, and its backups.

References such as "001 FR-028" point to requirements in `specs/001-repository-qa/spec.md`,
"002 FR-012" to `specs/002-push-reindexing/spec.md`, and "003 FR-027" to
`specs/003-pr-review/spec.md`.

### User Story 1 - Use CodeAtlas at a public address (Priority: P1)

A pilot user opens CodeAtlas's public address, signs in with GitHub, installs the pilot's GitHub
App on a repository, and uses every existing feature: indexing, browsing, search, questions, pull
request reviews, and automatic re-indexing on push. A visitor who is not allowed to use the pilot
can complete GitHub sign-in, but sees a page explaining that the pilot is by invitation, and
CodeAtlas keeps nothing of theirs except an audit record of the denied sign-in.

**Why this priority**: Everything else in this feature supports or measures this running service.

**Independent Test**: Following the operations guide, the developer builds the pilot environment
and releases the current main-branch commit. A pilot user signs in over HTTPS, connects a real
repository, pushes to it and sees the new commit indexed, asks a question, and reviews a pull
request. A second GitHub account that is not allowed is turned away.

**Acceptance Scenarios**:

1. **Given** the pilot is running, **When** anyone opens its address over plain HTTP, **Then**
   they are redirected to HTTPS, and the certificate is valid.
2. **Given** a pilot user, **When** they sign in, connect a repository, ask a question, and
   request a pull request review, **Then** each works as specified in 001, 002, and 003, with the
   real GitHub App and model providers.
3. **Given** a visitor who is not allowed to use the pilot, **When** they complete GitHub sign-in,
   **Then** they see a page explaining that the pilot is by invitation, no workspace is created,
   their GitHub token is not kept, and an audit event records the denied sign-in.
4. **Given** a connected repository, **When** a commit is pushed to its default branch, **Then**
   the pilot receives GitHub's notification and indexes the commit automatically (002 FR-001).
5. **Given** the pilot-wide daily question limit has been reached, **When** any pilot user submits
   a question, **Then** it is refused with the reset time, even if their own workspace allowance
   remains.
6. **Given** fake external services are enabled or a required credential is missing, **When** the
   pilot starts, **Then** it refuses to start and names the setting without showing any value.
7. **Given** anyone on the internet, **When** they try to reach the pilot's services, **Then** only
   the web app answers, at the pilot's public address; the API is reachable only through it, and
   the database and the worker cannot be reached at all.

---

### User Story 2 - Learn about failures and recover from them (Priority: P2)

The developer receives an email when the pilot is unreachable, when the worker stops, when jobs
pile up or keep failing, when a backup is missing, when a release fails, or when spending passes a
threshold. They can search recent logs from every service in one place and see each service's
resource use. The database is backed up nightly to storage separate from the database, and the
developer has rehearsed building a new environment from the recorded configuration and restoring
the latest backup into it.

**Why this priority**: Each service runs as a single instance, so one failure can take down the
whole service. Learning about it quickly and being able to rebuild is what makes that trade-off
acceptable. It ranks below User Story 1 because it protects a service that must exist first.

**Independent Test**: Stop the worker while jobs are queued and confirm that an alert that the
worker stopped arrives. Stop the web app and confirm that an unreachability alert arrives. Then
build a new environment from the recorded configuration, restore the latest backup into it, and
confirm that a pilot user's repositories, answers, and reviews are present, recording how long it
took.

**Acceptance Scenarios**:

1. **Given** the public address stops answering, **When** 5 minutes pass, **Then** the developer
   receives an alert, and another alert when it recovers.
2. **Given** the worker stops while jobs are queued, **When** it has reported nothing for 5
   minutes, **Then** the developer receives an alert that the worker stopped.
3. **Given** the worker runs but falls behind, **When** the oldest job that could run now has
   waited 30 minutes, **Then** the developer receives an alert. Jobs held back by the
   one-job-at-a-time rule of their workspace (001 FR-027) do not count.
4. **Given** jobs keep failing, **When** 3 jobs fail within one hour after using up their retries
   or with an internal error, **Then** the developer receives an alert. Failures caused by the
   input or by lost access, such as a repository over a size limit, do not count.
5. **Given** a request or job failed, **When** the developer searches the logs by its identifier,
   **Then** they find its log lines from every service, and none contains a token, source text, a
   prompt, or model output.
6. **Given** no backup has completed in the last 26 hours, **When** that mark passes, **Then** the
   developer receives an alert within about an hour.
7. **Given** the pilot's environment or its database is lost, **When** the developer follows the
   recovery procedure, **Then** a new environment serves the data from the latest backup within 2
   hours.
8. **Given** a service crashes or the hosting platform restarts it, **When** it comes back,
   **Then** it runs again without manual steps, and interrupted jobs resume or fail with a stated
   reason.
9. **Given** the month's hosting spend passes 80% of the budget, **When** the hosting platform
   records it, **Then** the developer receives an email.

---

### User Story 3 - Release changes safely (Priority: P3)

When a change is merged into the main branch and its automated checks pass, the pilot releases
that commit with no further steps: it applies schema changes, starts the new version, and moves
traffic to it only after the new version passes its readiness check. If any step fails, the
previous version keeps serving and the developer is alerted. The developer can also roll back any
service to its previous release in one action.

**Why this priority**: Releasing on every merge removes manual steps and makes every release
repeatable; the readiness check and the one-action rollback make it reversible. It ranks below
monitoring and recovery, which a release relies on when something goes wrong.

**Independent Test**: Merge a small visible change and confirm that the new version serves within
15 minutes and reports its commit. Then merge a change made to fail its readiness check, and
confirm that the previous version keeps serving and an alert arrives. Finally, roll back to the
previous release and confirm that it serves again within 5 minutes.

**Acceptance Scenarios**:

1. **Given** a commit on the main branch whose automated checks pass, **When** they finish,
   **Then** the pilot releases it without a manual approval, reports the new commit as its
   version, and jobs that were running during the release finish or resume, with none lost or
   duplicated.
2. **Given** a commit whose automated checks fail, **When** they finish, **Then** nothing is
   released.
3. **Given** a pull request, **When** its automated checks run, **Then** they report whether it
   changes the database schema, so that the change is checked in review before it is merged and
   released.
4. **Given** a new version that fails its readiness check, **When** the release reacts, **Then**
   the new version never receives traffic, the previous version keeps serving, and the developer
   is alerted.
5. **Given** a schema change that fails, **When** the release applies it, **Then** the release
   stops, the previous version keeps serving, and the developer is alerted.
6. **Given** a released version that misbehaves, **When** the developer rolls back, **Then** the
   previous release serves again within 5 minutes. Schema changes already applied stay in place,
   and the previous version works with them.
7. **Given** a proposed change to the hosting configuration, such as a larger service size,
   **When** it is prepared, **Then** the developer sees every change before it takes effect, and
   the recorded configuration is updated with it.
8. **Given** the repository and the CI service, **When** anyone inspects them, **Then** they hold
   no credentials for the hosting platform.

---

### User Story 4 - Publish measured results (Priority: P4)

A reader of the project's README sees a short demo recording, an architecture diagram of the
deployed system, its known limitations, and links to two reports: a load test at the pilot's
service sizes, and an evaluation of question answering and pull request review with the real
model providers. The evaluation includes a public review benchmark labeled by people, so that
CodeAtlas's reviews can be compared with other review tools on the same pull requests. Each report
states how it was measured, so that its numbers can be reproduced.

**Why this priority**: The reports show how the system behaves in practice. They rank last because
they describe the running system rather than change it.

**Independent Test**: Follow the load test report's steps on its recorded commit and confirm that
the run completes and writes a report in the same form. Confirm that every number in both
reports states its denominator and the commit and configuration it was measured with.

**Acceptance Scenarios**:

1. **Given** the load test report, **When** a reader opens it, **Then** it states the service
   sizes, configuration, commit, duration, and each concurrency level tested, and for each level
   the 95th percentile latency of searches, views, and submissions, and the error rate.
2. **Given** the evaluation report, **When** a reader opens it, **Then** it reports the
   benchmark's precision and recall next to other tools' published results on the same pull
   requests, naming the judge model, and each measured success criterion of 001 and 003 with
   denominators and per-item failures, the model cost and latency per item, and the review status
   of each evaluation set.
3. **Given** the README, **When** a reader opens it, **Then** the demo recording shows a user
   connecting a repository, asking where something is implemented, opening the cited code, and
   reviewing a pull request.
4. **Given** the operations guide, **When** the developer needs to act, **Then** it covers every
   procedure FR-030 lists.

### Edge Cases

- A pilot user is removed. Their sessions end at their next request. Their data stays until the
  developer deletes it by the documented procedure.
- A release restarts the worker while an indexing job runs. The job resumes after the release and
  finishes within its deadline, or fails with a timeout reason (001 FR-031).
- GitHub sends a push notification while the pilot is down, during a release or an outage. GitHub
  does not redeliver it; the daily check indexes the missed push (002 User Story 3).
- A schema change would break the running version, for example by removing a column it reads. It
  is split across two pull requests, released one after the other. The automated checks flag
  every schema change, so the developer checks it in review before merging.
- A schema change fails partway. The release stops, the previous version keeps serving, and the
  developer is alerted.
- The database's storage fills from indexes. Indexing that cannot finish fails with a stated
  reason, and the current version stays in use (001 FR-012); repeated failures raise the
  job-failure alert. The hosting platform's resource view shows storage use, and the operations
  guide covers enlarging it.
- The certificate for the public address is invalid or expired. The external check fails as it
  would for an outage, so the unreachability alert fires.
- One network address floods the sign-in or GitHub notification endpoints with requests. Requests
  over the rate limit are refused before any other processing. The limit counts the client's
  address as the hosting platform's edge reports it, not the address of the web app that forwards
  requests to the API.
- The pilot-wide limit is reached partway through a day. New questions and reviews are refused
  with the reset time; jobs already accepted finish; browsing and search continue.
- A user deletes data that a backup already holds. Deleted data is purged within 24 hours
  (001 FR-035), and each nightly backup run first deletes the backups dated before the last 7
  days, even when the backup itself then fails. The disclosure therefore says the data can remain
  in backups for up to 10 days; if the nightly runs stop, the backup alert fires after 26 hours.
- A GitHub account outside the pilot installs the pilot's public App. Its notifications are
  ignored (002 FR-010). CodeAtlas keeps only the delivery record that 002 keeps for every verified
  delivery, for 14 days, to drop duplicates: the delivery ID, event and action, installation and
  repository IDs, the outcome, and for pushes the branch and head commit.
- A fork's pull request, or a branch other than main, finishes its checks. Nothing is released.
- A backup is restored. Changes made after the backup, including disconnections, are lost. Access
  losses are detected again by the daily check (002 FR-012). The recovery procedure tells the
  developer to inform pilot users of the backup time, so that they can disconnect repositories
  again.
- A rebuilt environment gets a new public address unless it can take over the old one. The
  recovery procedure covers taking over the old address or, failing that, updating the GitHub
  App's callback and webhook addresses and telling pilot users the new address.
- The hosting platform has an outage. The pilot is down until the platform recovers. The external
  check runs outside the platform, so the unreachability alert still arrives.
- Spending passes 80% of the monthly hosting budget. The developer is alerted. A hard limit at
  twice the budget stops the services rather than let spending run past it.
- A benchmark repository exceeds the repository size limits of 001 FR-009. The evaluation raises
  the limits for its own run and reports them; the pilot keeps the default limits, so it would
  still refuse that repository.

## Requirements *(mandatory)*

### Functional Requirements

**Access and security**

- **FR-001**: CodeAtlas MUST be served over HTTPS only, at a stable public address that the
  hosting platform provides; the pilot MUST NOT need a domain name of the developer's own. Plain
  HTTP requests MUST be redirected to HTTPS. The certificate MUST renew without the developer's
  action.
- **FR-002**: Only GitHub accounts on an access list kept by the developer MUST be able to use the
  pilot. The list holds at most 10 accounts by default.
- **FR-003**: A GitHub user who may not use the pilot MUST see a page explaining that access is by
  invitation. System MUST NOT create a workspace for them or keep their GitHub token, and MUST
  record an audit event for the denied sign-in.
- **FR-004**: The developer MUST be able to add and remove pilot users without a new release.
  Removing a user MUST end their sessions at their next request.
- **FR-005**: System MUST enforce pilot-wide daily limits, across all workspaces, of 30 new
  questions and 15 new reviews, in addition to the workspace limits (001 FR-028, 003 FR-027).
  Requests over a pilot-wide limit MUST be refused with the reset time.
- **FR-006**: The endpoints reachable without a session, sign-in and GitHub notifications, MUST
  limit requests to 60 per minute from one client network address, as the hosting platform's edge
  reports it. Requests over the limit MUST be refused before any other processing.
- **FR-007**: The web app MUST be the only service with a public address. The API MUST be
  reachable from the internet only through the web app's address, and the database and the worker
  MUST NOT be reachable from the internet. Administrative access to the services MUST require the
  developer's hosting platform account.
- **FR-008**: Credentials (the GitHub App's private key and secrets, model provider keys, the token
  encryption key, the database password, and the backup storage keys) MUST be kept out of the
  repository and the release images, MUST be available only to the services that use them, and
  MUST be replaceable by a documented procedure.
- **FR-009**: The pilot MUST refuse to start when fake external services are enabled or when a
  credential it needs is missing. The error MUST name the setting and MUST NOT show any value.
- **FR-010**: The pilot MUST use its own GitHub App registration, with its own webhook secret,
  separate from the one used in development.

**Health, monitoring, and alerts**

- **FR-011**: System MUST report liveness separately from readiness. Readiness MUST fail when the
  database is unreachable, and MUST NOT fail when GitHub or a model provider is unavailable.
- **FR-012**: Logs from every service MUST be collected in one place, kept for 7 days, and
  searchable by request, job, run, and snapshot identifiers. Logs MUST NOT contain tokens, source
  text, prompts, or model output.
- **FR-013**: The developer MUST be able to view, for the last 7 days, each service's CPU, memory,
  and network use and the database's storage use. Every request MUST be logged with its kind,
  status, and duration. The worker MUST log, at least once a minute, the number of queued jobs and
  the age of the oldest that could run now, and MUST log each job's duration, retries, and outcome
  with its kind. Model provider errors MUST be logged with the provider and the kind of error.
- **FR-014**: System MUST alert the developer by email when the public address fails the external
  check for 5 minutes, the worker reports nothing for 5 minutes, the oldest job that could run now
  has waited 30 minutes, 3 jobs fail within one hour after using up their retries or with an
  internal error, no backup has completed in the last 26 hours, a release fails (in its build, its
  schema change, or its readiness check), or the month's hosting spend passes 80% of the budget.
  The external check MUST run outside the hosting platform. An alert about a failing condition
  MUST be followed by a notice when it recovers; release and spending alerts, which report single
  events, are exempt.

**Backups and recovery**

- **FR-015**: The database MUST be backed up at least daily to private, encrypted storage separate
  from the database's own storage. Backups MUST be kept for 7 days.
- **FR-016**: The hosting configuration MUST be recorded with the code: each service's build,
  start, schedule, and readiness settings, its size, and the names (not the values) of the
  settings and credentials it needs. A new environment MUST be buildable from that record, and
  restorable from the latest backup, by following the operations guide.
- **FR-017**: After a service crashes or the hosting platform restarts it, the service MUST run
  again without manual steps, and interrupted jobs MUST resume or fail with a stated reason
  (001 FR-030 and SC-010).
- **FR-018**: The external processing disclosure (001 FR-006) MUST state that deleted data can
  remain in encrypted backups for up to 10 days after deletion.

**Releases**

- **FR-019**: A release MUST be built from one commit pushed to this repository's main branch, and
  MUST start only after that commit's automated checks have passed. Commits from forks or other
  branches MUST NOT start a release. Every release MUST be labeled with its commit, and the pilot
  MUST report the commit it runs.
- **FR-020**: A commit MUST be released without a manual approval once its automated checks pass.
  On every pull request, the automated checks MUST report whether it changes the database schema.
- **FR-021**: A release MUST apply schema changes before starting the new version. Schema changes
  MUST keep the running version working, so that it can be restored. A failed schema change MUST
  stop the release, with the previous version still serving.
- **FR-022**: A new version of a service that serves requests MUST receive traffic only after it
  passes its readiness check. When the check fails, the new version MUST NOT receive traffic, the
  previous version MUST keep serving, and the developer MUST be alerted.
- **FR-023**: A release MUST NOT lose or duplicate jobs, and MUST interrupt service for at most 60
  seconds.
- **FR-024**: While the hosting platform still holds the previous release (at least 72 hours after
  it was replaced), the developer MUST be able to roll back each service to it in one action, and
  the previous release MUST serve again within 5 minutes. After that, rolling back redeploys the
  earlier commit, which MUST serve within 15 minutes.
- **FR-025**: The repository and the CI service MUST hold no credentials for the hosting
  platform. Changes to the hosting configuration MUST be shown to the developer in full before
  they take effect, and the recorded configuration (FR-016) MUST be updated with them.

**Measurements and documentation**

- **FR-026**: The load test MUST run against a temporary environment copied from the pilot's
  configuration, with the same service sizes and the same release, fake external services, raised
  limits, and no pilot data, and the environment MUST be deleted afterward. It MUST measure the
  categories of 001 SC-007 at 10 concurrent users, then at increasing concurrency until a target is
  missed or errors appear, and MUST report the highest concurrency that meets every target, or that
  every tested level met them.
- **FR-027**: The evaluation MUST review the Python and TypeScript pull requests of a public pull
  request review benchmark whose expected findings were written and verified by people. It MUST
  score the reviews with the benchmark's own matching method and, while one is still served, one
  of the judge models behind its published results, and MUST report precision and recall next to
  the published results of other review tools on the same pull requests. If none of those judges
  is served, the report MUST re-judge the compared tools' published candidates with the judge it
  uses and say so. The run MAY raise the repository size limits of 001 FR-009; the report MUST
  state the limits used.
- **FR-028**: The evaluation MUST run the question set and the pull request review set with the
  real model providers, and report 001 SC-003, SC-004, and SC-006 and 003 SC-002, SC-003, SC-005,
  and SC-007. Every evaluation number MUST come with its denominator, per-item failures, model
  cost and latency per item, and the commit and set versions used. Numbers from the question set
  MUST be labeled as measured on a set that no person has reviewed yet, and numbers from the
  review set as measured on a set reviewed only by a model.
- **FR-029**: The README MUST include a demo recording of at most 60 seconds, an architecture
  diagram of the deployed system, the known limitations, and links to both reports and the
  operations guide.
- **FR-030**: The operations guide MUST cover building the environment, rolling back a release,
  restoring a backup, recovering from the loss of the environment or its database, replacing each
  credential, adding and removing pilot users, deleting a removed user's data, and tearing the
  environment down.
- **FR-031**: The known limitations MUST state that each service runs as a single instance on one
  hosting platform, so a failure of a service or of the platform interrupts the pilot; that
  releases interrupt service for up to 60 seconds; that up to 24 hours of changes can be lost (more
  if a failed nightly backup is not fixed before the next); that the backups are held by the same
  hosting account as the database, so losing that account loses both; and that GitHub
  notifications sent while the pilot is down are caught only by the daily check.

### Key Entities *(include if feature involves data)*

- **Pilot user**: A GitHub account allowed to use the pilot. Added and removed by the developer,
  with the date it was added.
- **Release**: A deployment of one commit, kept in the hosting platform's deployment history. Has
  the commit, whether it changes the schema, start and end times, and an outcome: released, failed
  before serving (in its build, its schema change, or its readiness check), or replaced by a
  rollback.
- **Backup**: A daily copy of the database. Has its time, its size, its checksum, and the row
  counts used to verify a restore.
- **Pilot-wide usage allowance**: The daily counts and limits of questions and reviews across all
  workspaces.
- **Audit event** (extended): Covers denied sign-ins and changes to the pilot users.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Over 14 consecutive days after launch, the external check reports the public address
  healthy at least 99% of the time, averaged over the period.
- **SC-002**: Hosting costs for the first full month, excluding model providers, are at most $25.
- **SC-003**: At the default pilot-wide limits, the largest model provider spend in one day,
  computed from the limits and the per-request token caps with one billed attempt per request, is
  at most $15, and measured daily spend in the first month stays below it.
- **SC-004**: 100% of sign-in attempts by GitHub accounts that may not use the pilot are denied,
  with no workspace created and no token kept.
- **SC-005**: From outside the hosting platform, 100% of connection attempts to the database, the
  API, and the worker fail, and the pilot's public address answers only on the web ports.
- **SC-006**: A scan of the repository history and 7 days of logs finds zero credentials, and no
  credential is available to the build of a release.
- **SC-007**: In fault tests (stopping the web app, stopping the worker while jobs are queued,
  making 3 jobs fail with a failure that counts, such as exceeding their deadline, and releasing a
  version that fails its readiness check), the developer receives the matching alert within 10
  minutes of the condition being met for 100% of the faults. With the nightly backup disabled, the
  backup alert arrives within an hour after the 26-hour mark.
- **SC-008**: In a recovery exercise, a new environment built from the recorded configuration and
  restored from the latest backup serves the restored data within 2 hours of starting, losing only
  changes made after that backup.
- **SC-009**: Across at least 5 releases, each serves the new version within 15 minutes of its
  merge, with at most 60 seconds of interruption, and zero lost or duplicated jobs.
- **SC-010**: In a release whose new version is made to fail its readiness check, the previous
  version serves throughout. A rollback to the previous release serves it again within 5 minutes.
- **SC-011**: At the pilot's service sizes with 10 concurrent users, the latency targets of 001
  SC-007 are met, and the load test report states the highest concurrency at which they still
  hold.
- **SC-012**: The evaluation report covers all benchmark pull requests in Python and TypeScript
  and every success criterion listed in FR-028, each number with its denominator and review status.

## Assumptions

- This feature is the fourth increment of CodeAtlas. It covers the deployment, operations,
  recovery, and measurement parts of milestone M4 in `docs/design/codeatlas-v1.md`, and builds on
  `specs/001-repository-qa`, `specs/002-push-reindexing`, and `specs/003-pr-review`.
- Every service, the database, and its backups run on one managed application platform, each
  service as a single instance in one region. The developer chose it over a self-managed cloud
  host because it needs no host administration and no domain name, and its configuration is far
  smaller to record. The pilot is therefore not highly available. The plan records the platform,
  this trade-off, and the signals for moving elsewhere in an ADR that supersedes the earlier
  single-host decisions.
- There is no standing staging environment, to save cost. Releases go to the pilot after the
  automated checks, which already include browser tests in fake mode. The load test and the
  recovery exercise use temporary environments copied from the same configuration. This replaces
  the staging smoke test in the design document.
- The pilot serves at most 10 pilot users, chosen by the developer. Everyone else learns about
  CodeAtlas from the README and its demo recording.
- The review benchmark is Code Review Bench (`withmartian/code-review-benchmark`, MIT license). Its
  Python and TypeScript part has 20 pull requests with 77 expected findings; its Go, Ruby, and Java
  pull requests are out of scope because CodeAtlas extracts declarations only from Python and
  TypeScript. Its pull requests come from well-known public repositories, so models may have seen
  them during training; the report says so. Scoring with its judge needs one more model provider
  key, used only by the evaluation.
- The human reviews of the question set and the review set, and the human audits of 001 SC-005 and
  003 SC-004, are deferred. The evaluation report labels its numbers accordingly.
- The pilot uses the address the hosting platform provides; the developer buys no domain name. A
  domain can be added later without changing the application.
- The developer pays for the hosting platform and the model providers, and receives all alerts by
  email.
- The external check, which detects outages and reads the worker, queue, and backup conditions of
  FR-014, runs on a free monitoring service outside the hosting platform, because a check on the
  same platform would go down with it. Release and spending alerts come from the hosting platform.
- The external model services are unchanged from 001 and 003.
- Writes to external systems are developer actions: merges into the main branch (each of which
  releases), changes to the hosting configuration, GitHub App registration and settings, and
  monitoring and billing settings. An agent may prepare commands and summaries, and runs them only
  after the developer confirms.
- Logs and backups are kept for 7 days, as in the design document's defaults. The nightly backup
  starts at 03:30 UTC.
- Out of scope: high availability, multiple regions, autoscaling, zero-downtime releases, a
  standing staging environment, an operations interface for jobs, self-service account deletion,
  team workspaces, a content delivery network, a custom domain, and a separate metrics system.
  Posting reviews to GitHub remains the next feature.
- The numeric limits and thresholds in this spec are starting values and remain configurable.
- GitHub availability and rate limits, and model provider availability, are external dependencies,
  as in 001, 002, and 003.
