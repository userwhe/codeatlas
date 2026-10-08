# Feature Specification: Pilot Deployment

**Feature Branch**: `004-pilot-deployment`

**Created**: 2026-10-07

**Status**: Draft

**Input**: User description (translated): "Build 004, deployment." In the same discussion, the
developer chose to run every service on one cloud host on AWS instead of managed container and
database services, to keep hosting costs low.

## User Scenarios & Testing *(mandatory)*

This is the fourth increment of CodeAtlas. The first three increments (`specs/001-repository-qa`,
`specs/002-push-reindexing`, `specs/003-pr-review`) run on a developer's machine. This feature
runs CodeAtlas as a pilot service on the public internet. Invited users sign in at a public
address and use it on their own GitHub repositories. The developer releases changes safely,
learns about failures from alerts, and can rebuild the service if its host is lost. The feature
also measures the deployed system: a load test on the pilot's hardware, and an evaluation with
the real model providers.

A **pilot user** is someone allowed to use the pilot. A **visitor** is anyone else who signs in.
The **developer** is the person who operates CodeAtlas.

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
and releases the current main-branch commit by hand. A pilot user signs in over HTTPS, connects a
real repository, pushes to it and sees the new commit indexed, asks a question, and reviews a pull
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
7. **Given** anyone on the internet, **When** they probe the host, **Then** only the web ports
   answer; the database and administrative access to the host do not.

---

### User Story 2 - Learn about failures and recover from them (Priority: P2)

The developer receives an email when the pilot is unreachable, when jobs pile up or keep failing,
when disk space runs low, when a backup is missing, or when spending is on track to exceed the
budget. They can read recent logs and metrics in one place to find the cause. The database is
backed up daily to storage outside the host, and the developer has rehearsed building a new
environment from its recorded definition and restoring the latest backup into it.

**Why this priority**: With one host, a host failure takes down the whole service. Learning about
it quickly and being able to rebuild is what makes that trade-off acceptable. It ranks below User
Story 1 because it protects a service that must exist first.

**Independent Test**: Stop the worker while jobs are queued and confirm that an alert that the
worker stopped arrives. Stop the edge proxy, and then the web app, and confirm that an
unreachability alert arrives each time. Then build a new environment from the definition, restore
the latest backup into it, and confirm that a pilot user's repositories, answers, and reviews are
present, recording how long it took.

**Acceptance Scenarios**:

1. **Given** the public address or the web app stops answering, **When** 5 minutes pass,
   **Then** the developer receives an alert, and another alert when it recovers.
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
7. **Given** the host is lost, **When** the developer follows the recovery procedure, **Then** a new
   environment serves the data from the latest backup within 2 hours.
8. **Given** the host restarts unexpectedly, **When** it comes back, **Then** every service starts
   without manual steps, and interrupted jobs resume or fail with a stated reason.

---

### User Story 3 - Release changes safely (Priority: P3)

When a change is merged into the main branch, the release pipeline runs the automated checks,
builds a release from that commit, and prepares a release summary: the commit, the commits since
the running version, and whether the database schema changes. The developer approves the release.
It applies schema changes, starts the new version, and checks that it works. If the check fails,
the previous version is restored automatically and the developer is alerted.

**Why this priority**: Releasing by hand from the operations guide already works (User Story 1).
Automation removes manual steps and makes every release repeatable and reversible, so it ranks
below monitoring and recovery.

**Independent Test**: Merge a small visible change, approve its release, and confirm that the new
version serves within 15 minutes and reports its commit. Then release a build made to fail its
post-release check, and confirm that the previous version is restored and an alert arrives.

**Acceptance Scenarios**:

1. **Given** a commit on the main branch whose automated checks pass, **When** the pipeline runs,
   **Then** it prepares a release summary and waits; nothing in the pilot changes until the
   developer approves.
2. **Given** a commit whose automated checks fail, **When** the pipeline runs, **Then** no release
   is prepared.
3. **Given** an approved release, **When** it finishes, **Then** the pilot reports the new commit
   as its version, and jobs that were running during the release finish or resume, with none lost
   or duplicated.
4. **Given** the post-release check fails, **When** the release reacts, **Then** the previous
   version serves again within 5 minutes and the developer is alerted. Schema changes already
   applied stay in place, and the previous version works with them.
5. **Given** a proposed change to the hosting environment itself, such as a larger host, **When**
   it is prepared, **Then** the developer sees a preview of every change, and nothing is applied
   until they approve it.
6. **Given** the release pipeline, **When** it acts on the cloud account, **Then** it uses
   short-lived credentials, and no long-lived cloud keys are stored in the repository or the CI
   settings.

---

### User Story 4 - Publish measured results (Priority: P4)

A reader of the project's README sees a short demo recording, an architecture diagram of the
deployed system, its known limitations, and links to two reports: a load test on the pilot's
hardware, and an evaluation of question answering and pull request review with the real model
providers. The evaluation includes a public review benchmark labeled by people, so that
CodeAtlas's reviews can be compared with other review tools on the same pull requests. Each report
states how it was measured, so that its numbers can be reproduced.

**Why this priority**: The reports show how the system behaves in practice. They rank last because
they describe the running system rather than change it.

**Independent Test**: Follow the load test report's steps on its recorded commit and confirm that
the run completes and writes a report in the same form. Confirm that every number in both
reports states its denominator and the commit and configuration it was measured with.

**Acceptance Scenarios**:

1. **Given** the load test report, **When** a reader opens it, **Then** it states the hardware,
   configuration, commit, duration, and each concurrency level tested, and for each level the
   95th percentile latency of searches, views, and submissions, and the error rate.
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
- A release starts while an indexing job runs. The job resumes after the release and finishes
  within its deadline, or fails with a timeout reason (001 FR-031).
- GitHub sends a push notification while the pilot is down, during a release or an outage. GitHub
  does not redeliver it; the daily check indexes the missed push (002 User Story 3).
- A schema change would break the running version, for example by removing a column it reads. It
  is split across two releases. The release summary flags every schema change so the developer
  can check it before approving.
- A schema change fails partway. The release stops, the previous version keeps serving, and the
  developer is alerted.
- Disk space runs low from indexes or logs. The developer is alerted at 80% use. Indexing that
  cannot finish fails with a stated reason, and the current version stays in use (001 FR-012).
- The certificate cannot be renewed. The developer is alerted once fewer than 14 days remain.
- One network address floods the sign-in or GitHub notification endpoints with requests. Requests
  over the rate limit are refused before any other processing.
- The pilot-wide limit is reached partway through a day. New questions and reviews are refused
  with the reset time; jobs already accepted finish; browsing and search continue.
- A user deletes data that a backup already holds. Deleted data is purged within 24 hours
  (001 FR-035); a backup expires 7 days after it is taken, rounded up to the next midnight UTC,
  and S3 removes it shortly after. The disclosure therefore says the data can remain in backups
  for up to 10 days.
- A GitHub account outside the pilot installs the pilot's public App. Its notifications are
  ignored (002 FR-010). CodeAtlas keeps only the delivery record that 002 keeps for every verified
  delivery, for 14 days, to drop duplicates: the delivery ID, event and action, installation and
  repository IDs, the outcome, and for pushes the branch and head commit.
- A fork's pull request, or a branch other than main, finishes its checks. No release is
  prepared.
- A backup is restored. Changes made after the backup, including disconnections, are lost. Access
  losses are detected again by the daily check (002 FR-012). The recovery procedure tells the
  developer to inform pilot users of the backup time, so that they can disconnect repositories
  again.
- A rebuilt host has a new network address. The public address and GitHub's notification address
  keep working, because they use the domain name.
- Spending is on track to exceed the monthly hosting budget. The developer is alerted when the
  forecast passes 80% of the budget.
- A benchmark repository exceeds the repository size limits of 001 FR-009. The evaluation raises
  the limits for its own run and reports them; the pilot keeps the default limits, so it would
  still refuse that repository.

## Requirements *(mandatory)*

### Functional Requirements

**Access and security**

- **FR-001**: CodeAtlas MUST be served at a stable public address over HTTPS only. Plain HTTP
  requests MUST be redirected to HTTPS. The certificate MUST renew automatically.
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
  limit requests to 60 per minute from one network address. Requests over the limit MUST be refused
  before any other processing.
- **FR-007**: Only the web ports MUST be reachable from the internet. The database and
  administrative access to the host MUST NOT be.
- **FR-008**: Credentials (the GitHub App's private key and secrets, model provider keys, the token
  encryption key, and the database password) MUST be kept out of the repository and the release
  artifacts, MUST be readable only by the services that use them, and MUST be replaceable by a
  documented procedure.
- **FR-009**: The pilot MUST refuse to start when fake external services are enabled or when a
  credential it needs is missing. The error MUST name the setting and MUST NOT show any value.
- **FR-010**: The pilot MUST use its own GitHub App registration, with its own webhook secret,
  separate from the one used in development.

**Health, monitoring, and alerts**

- **FR-011**: System MUST report liveness separately from readiness. Readiness MUST fail when the
  database is unreachable, and MUST NOT fail when GitHub or a model provider is unavailable.
- **FR-012**: Logs from every service MUST be collected in one place outside the host, kept for 7
  days, and searchable by request, job, run, and snapshot identifiers. Logs MUST NOT contain
  tokens, source text, prompts, or model output.
- **FR-013**: System MUST record metrics for: requests, errors, and latency by kind of request;
  the number of queued jobs and the age of the oldest; job duration, retries, and failures by kind
  of job; model provider errors; and host CPU, memory, and disk use. The developer MUST be able to
  view the last 7 days of these metrics in one view.
- **FR-014**: System MUST alert the developer by email when the public address or the web app
  fails external checks for 5 minutes, the worker reports nothing for 5 minutes, the oldest job
  that could run now has waited 30 minutes, 3 jobs fail within one hour after using up their
  retries or with an internal error, disk use exceeds 80%, no backup has completed in the last 26
  hours, the certificate expires within 14 days, or monthly hosting spend, actual or forecast,
  exceeds 80% of the budget. An alert about a failing condition MUST be followed by a notice when
  it recovers; spending alerts, which the billing service sends once per threshold, are exempt.

**Backups and recovery**

- **FR-015**: The database MUST be backed up at least daily to encrypted storage outside the host.
  Backups MUST be kept for 7 days.
- **FR-016**: The hosting environment MUST be described by a recorded definition kept with the
  code, so that a new environment can be built from it and restored from the latest backup by
  following the operations guide.
- **FR-017**: After the host restarts, every service MUST start without manual steps, and
  interrupted jobs MUST resume or fail with a stated reason (001 FR-030 and SC-010).
- **FR-018**: The external processing disclosure (001 FR-006) MUST state that deleted data can
  remain in encrypted backups for up to 10 days after deletion.

**Releases**

- **FR-019**: A release MUST be built from one commit pushed to this repository's main branch
  whose automated checks passed. Commits from forks or other branches MUST NOT start a release.
  Every release artifact MUST be labeled with that commit, and the pilot MUST report the commit it
  runs.
- **FR-020**: Before a release writes anything to the cloud account (images, files, or the
  running services), System MUST present a release summary (the commit, the commits since the
  running version, and whether the database schema changes) and MUST proceed only after the
  developer approves it. An approval covers only the commit in that summary.
- **FR-021**: A release MUST apply schema changes before starting the new version. Schema changes
  MUST keep the running version working, so that it can be restored. A failed schema change MUST
  stop the release, with the previous version still serving.
- **FR-022**: After starting the new version, a release MUST check that the public address serves
  the web app, readiness passes, and the reported version is the released commit. When the check
  fails, the previous version MUST be restored automatically and the developer alerted.
- **FR-023**: A release MUST NOT lose or duplicate jobs, and MUST interrupt service for at most 60
  seconds.
- **FR-024**: The release pipeline MUST act on the cloud account only with short-lived credentials
  limited to what releases need. No long-lived cloud keys may be stored in the repository or the CI
  settings.
- **FR-025**: Changes to the hosting environment MUST be previewed, and MUST be applied only after
  the developer approves the preview.

**Measurements and documentation**

- **FR-026**: The load test MUST run against a temporary environment built from the pilot's
  definition, with the same host size, services, and release images, fake external services, raised
  limits, and no pilot data, and the environment MUST be torn down afterward. It MUST measure the
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
- **FR-030**: The operations guide MUST cover building the environment, releasing and rolling back
  by hand, restoring a backup, recovering from host loss, replacing each credential, adding and
  removing pilot users, deleting a removed user's data, and tearing the environment down.
- **FR-031**: The known limitations MUST state that the single host is a single point of failure,
  that releases interrupt service for up to 60 seconds, that up to 24 hours of changes can be lost
  (more if a failed nightly backup is not fixed before the next), and that GitHub notifications
  sent while the pilot is down are caught only by the daily check.

### Key Entities *(include if feature involves data)*

- **Pilot user**: A GitHub account allowed to use the pilot. Added and removed by the developer,
  with the date it was added.
- **Release**: A deployment of one commit. Has the commit, the schema changes it includes, who
  approved it, start and end times, and an outcome: released, rolled back, stopped before any
  change, or rollback failed.
- **Backup**: A daily copy of the database. Has its time, its size, its checksum, and the row
  counts used to verify a restore.
- **Pilot-wide usage allowance**: The daily counts and limits of questions and reviews across all
  workspaces.
- **Audit event** (extended): Covers denied sign-ins and changes to the pilot users.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Over 14 consecutive days after launch, the external check reports the public address
  healthy at least 99% of the time, averaged over the period.
- **SC-002**: Hosting costs for the first full month, excluding model providers and the domain
  name, are at most $40.
- **SC-003**: At the default pilot-wide limits, the largest model provider spend in one day,
  computed from the limits and the per-request token caps with one billed attempt per request, is
  at most $15, and measured daily spend in the first month stays below it.
- **SC-004**: 100% of sign-in attempts by GitHub accounts that may not use the pilot are denied,
  with no workspace created and no token kept.
- **SC-005**: An external scan of the host finds only the web ports open.
- **SC-006**: A scan of the repository history, the release artifacts, and 7 days of logs finds
  zero credentials.
- **SC-007**: In fault tests (stopping the edge proxy, stopping the web app, stopping the worker
  while jobs are queued, and filling the disk past the threshold), the developer receives the
  matching alert within 10 minutes for 100% of the faults. With the backup timer disabled, the
  backup alert arrives within an hour after the 26-hour mark.
- **SC-008**: In a recovery exercise, a new environment built from the recorded definition and
  restored from the latest backup serves the restored data within 2 hours of starting, losing only
  changes made after that backup.
- **SC-009**: Across at least 5 releases, each serves the new version within 15 minutes of
  approval, with at most 60 seconds of interruption, and zero lost or duplicated jobs.
- **SC-010**: In a release whose post-release check is made to fail, the previous version serves
  again within 5 minutes.
- **SC-011**: On the pilot's hardware with 10 concurrent users, the latency targets of 001 SC-007
  are met, and the load test report states the highest concurrency at which they still hold.
- **SC-012**: The evaluation report covers all benchmark pull requests in Python and TypeScript
  and every success criterion listed in FR-028, each number with its denominator and review status.

## Assumptions

- This feature is the fourth increment of CodeAtlas. It covers the deployment, operations,
  recovery, and measurement parts of milestone M4 in `docs/design/codeatlas-v1.md`, and builds on
  `specs/001-repository-qa`, `specs/002-push-reindexing`, and `specs/003-pr-review`.
- Every service runs on one cloud host, chosen by the developer over managed container and
  database services to keep hosting costs low at pilot scale. The pilot is therefore not highly
  available. The plan records the provider, this trade-off, and the signals for moving to managed
  services in an ADR.
- There is no standing staging environment, to save cost. Releases go to the pilot after the
  automated checks, which already include browser tests in fake mode. The load test and the
  recovery exercise use temporary environments built from the same definition. This replaces the
  staging smoke test in the design document.
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
- The developer provides a domain name; its cost is excluded from SC-002.
- The developer pays for the cloud account and the model providers, and receives all alerts by
  email.
- The external model services are unchanged from 001 and 003.
- Writes to external systems are developer actions: releases, changes to the hosting environment,
  GitHub App registration and settings, domain records, and billing settings. An agent may prepare
  commands and summaries, and runs them only after the developer confirms.
- Logs and backups are kept for 7 days, as in the design document's defaults. The nightly backup
  starts at 03:30 UTC.
- Out of scope: high availability, multiple regions, autoscaling, zero-downtime releases, a
  standing staging environment, an operations interface for jobs, self-service account deletion,
  team workspaces, and a content delivery network. Posting reviews to GitHub remains the next
  feature.
- The numeric limits and thresholds in this spec are starting values and remain configurable.
- GitHub availability and rate limits, and model provider availability, are external dependencies,
  as in 001, 002, and 003.
