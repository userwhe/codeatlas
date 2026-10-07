# Feature Specification: Automatic Re-indexing and Access Revocation

**Feature Branch**: `002-push-reindexing`

**Created**: 2026-10-05

**Status**: Draft

**Input**: User description (translated): "Automatically re-index a repository after a push to its
default branch, and check whether access has been revoked when re-indexing."

## User Scenarios & Testing *(mandatory)*

This is the second increment of CodeAtlas. In the first increment (`specs/001-repository-qa`), a
connected repository changes only when its owner re-indexes it by hand, and lost GitHub access is
noticed only at that moment. This feature keeps each connected repository current with its default
branch, and stops CodeAtlas from serving code that the workspace owner can no longer read on
GitHub.

References such as "001 FR-012" point to requirements in `specs/001-repository-qa/spec.md`.

### User Story 1 - Keep a repository current after pushes (Priority: P1)

A developer has connected a repository. Commits are pushed to its default branch. Without any
action from the developer, CodeAtlas indexes the new commit, and browsing, search, and new
questions use it as soon as it is ready. The repository page shows that the version was created
automatically after a push.

**Why this priority**: Answers about code that has since changed undermine trust, and manual
re-indexing is easy to forget. This is the main value of the feature.

**Independent Test**: Connect a fixture repository, push a commit that adds a new function to its
default branch, and confirm that, without any user action, the function becomes searchable and the
default version shows the new commit, marked as created by a push.

**Acceptance Scenarios**:

1. **Given** a connected repository with a ready version, **When** a commit is pushed to its
   default branch, **Then** indexing starts without user action, and when it finishes the new
   commit becomes the default version, marked as created by a push.
2. **Given** a connected repository, **When** a commit is pushed to any other branch, or a tag is
   pushed, **Then** no indexing starts.
3. **Given** several pushes to the default branch in quick succession, **When** CodeAtlas
   processes them, **Then** at most one run waits for the repository, and the last pushed commit
   becomes the default version.
4. **Given** a run is indexing commit A, **When** commit B is pushed, **Then** exactly one
   follow-up run indexes B after the first run ends, and B becomes the default version.
5. **Given** GitHub delivers the same notification more than once, **When** CodeAtlas receives the
   repeats, **Then** they create no additional runs.
6. **Given** an automatic run fails, for example on a size limit or a timeout, **When** the user
   opens the repository, **Then** the previous ready version is still the default, and the page
   shows that the latest push was not indexed and why.
7. **Given** an answer produced at commit A, **When** a push makes commit B the default version,
   **Then** the answer still states commit A and its citations show commit A content.
8. **Given** a notification that cannot be verified as sent by GitHub to CodeAtlas, **When**
   CodeAtlas receives it, **Then** it is rejected, and nothing is fetched, queued, or changed.

---

### User Story 2 - Stop serving a repository after GitHub access is lost (Priority: P1)

The workspace owner loses access to a connected repository on GitHub. For example, they are
removed from the organization, the CodeAtlas app is uninstalled or suspended, the repository is
removed from the installation, or the repository is deleted. CodeAtlas detects this, immediately
stops serving the repository's content, stops its work, and shows the repository as "access lost"
with the reason.

**Why this priority**: CodeAtlas stores copies of private code. A person who can no longer read a
repository on GitHub must not keep reading it through CodeAtlas. Automatic re-indexing must not
keep fetching code on behalf of someone who lost access.

**Independent Test**: Connect a fixture repository, remove the owner's access on GitHub, push a
commit, and confirm that the run stops before fetching anything, the repository shows "access
lost", and every page, search, and answer for it is denied.

**Acceptance Scenarios**:

1. **Given** the owner has lost access on GitHub, **When** a push starts an automatic run, **Then**
   the run stops before fetching any content, the repository is shown as "access lost" with the
   reason, and its versions, files, search results, and answers are all denied.
2. **Given** the CodeAtlas app is uninstalled or suspended, or the repository is removed from the
   installation, **When** GitHub notifies CodeAtlas, **Then** the affected repositories are marked
   "access lost" and their content is denied, without waiting for a push.
3. **Given** a run is in progress, **When** access loss is detected for its repository, **Then** the
   run cannot publish its result, and queued work for the repository is canceled.
4. **Given** GitHub is unavailable or rate-limiting during an access check, **When** the check
   fails, **Then** the repository is not marked "access lost", its content remains available, and
   the check is retried later.
5. **Given** a repository marked "access lost", **When** access returns within the 7-day grace
   period and a later check, push, or re-index request confirms it, **Then** the repository becomes
   readable again with its versions and answers, without re-indexing, and automatic runs resume.
6. **Given** a repository marked "access lost", **When** the 7-day grace period ends without access
   returning, **Then** the repository is disconnected and its data is purged, as in 001 FR-035.

---

### User Story 3 - Catch missed changes with a daily check (Priority: P2)

Some changes produce no notification. The owner may be removed from an organization, or a push
notification may be lost while CodeAtlas is down. Once a day, CodeAtlas checks each connected
repository. It confirms the owner's access, and if the default branch has moved past the latest
indexed commit, it indexes the new head.

**Why this priority**: Without the check, a repository that receives no pushes is never rechecked,
and a lost notification leaves the repository out of date until the next push. Pushes and GitHub
notifications already cover the common cases, so this story ranks below the first two.

**Independent Test**: Remove the owner's access to one fixture repository without a notification,
and push to a second while notifications are switched off. Run the daily check and confirm that
the first repository shows "access lost" and the second indexes the missed commit.

**Acceptance Scenarios**:

1. **Given** the owner lost access and GitHub sent no notification, **When** the daily check runs,
   **Then** the repository is marked "access lost", as in User Story 2.
2. **Given** a push whose notification never arrived, **When** the daily check runs, **Then** the
   default branch head is indexed, marked as created by the daily check.
3. **Given** access is intact and the default branch head matches the latest indexed commit,
   **When** the daily check runs, **Then** nothing is indexed, and only the time of the last
   successful check changes.
4. **Given** the owner's GitHub authorization has lapsed and cannot be renewed, **When** the daily
   check runs, **Then** the repository is not marked "access lost"; automatic updates are shown as
   paused until the owner signs in again, and they resume after the owner signs in.

### Edge Cases

- The default branch is renamed or changed on GitHub. Pushes to the new default branch start runs,
  and the repository shows the new branch name.
- A force push moves the default branch to an older commit. The new head is indexed and becomes the
  default version. If that commit already has a ready version, it is reused, as in the first
  increment.
- A push makes the repository exceed a size limit. The run fails with the limit named, the previous
  version stays the default, and later pushes are still processed.
- A notification arrives for a repository that is not connected in any workspace. It is ignored,
  and nothing is fetched.
- The same GitHub repository is connected in two workspaces. Each workspace's run checks its own
  owner's access and stores its own data. Access loss in one workspace does not affect the other.
- A public repository becomes private. Automatic updates pause until the owner accepts the
  external processing disclosure (001 FR-006).
- A repository is archived on GitHub. It receives no pushes and stays readable while access
  remains.
- A push arrives while the repository is being disconnected. No run publishes a result
  (001 FR-034).
- A push arrives for a repository marked "access lost". CodeAtlas checks access again; if access
  has returned within the grace period, the repository is restored and the push is indexed.
- The workspace already has indexing work running. Automatic runs wait in the visible queue
  (001 FR-027).
- The user requests a re-index while an automatic run for the same branch is waiting. Both
  requests are served by one run.
- A notification reports an installation that no connected repository uses. It is recorded and has
  no other effect.

## Requirements *(mandatory)*

### Functional Requirements

**Automatic re-indexing**

- **FR-001**: When a commit is pushed to the default branch of a connected repository, System MUST
  start indexing that branch without user action. The default branch is the one GitHub reports at
  the time of the push. Pushes to other branches, and tag pushes, MUST NOT start indexing.
- **FR-002**: System MUST act only on change notifications verified as sent by GitHub to CodeAtlas.
  Unverified notifications MUST be rejected without fetching, queuing, or changing anything, and
  MUST be recorded.
- **FR-003**: A run MUST index the branch head that GitHub reports when the run starts, regardless
  of the commit named in a notification or the order in which notifications arrive.
- **FR-004**: Each repository MUST have at most one waiting automatic run. A push that arrives while
  a run is waiting MUST be covered by that run. A push that arrives while a run is indexing an
  older commit MUST cause exactly one follow-up run after the current run ends.
- **FR-005**: Repeated delivery of the same notification MUST NOT create additional runs. A manual
  re-index request and a waiting automatic run for the same branch MUST be served by one run.
- **FR-006**: When runs for the same repository finish out of order, the default version MUST be
  the result of the most recently started successful run. An older run MUST NOT replace it.
- **FR-007**: Automatic runs MUST follow the same limits, exclusions, timeouts, retries, and failure
  handling as manual indexing (001 FR-009 to FR-013, FR-030, FR-031). A failed automatic run MUST
  leave the previous ready version as the default.
- **FR-008**: Automatic runs MUST count toward the workspace limit of one indexing job at a time,
  and MUST wait in the visible queue (001 FR-027).
- **FR-009**: Each version and each run MUST show what started it: the user, a push, or the daily
  check. The repository page MUST show when the latest push was received, the state of its run
  (waiting, indexing, indexed, or failed with a reason), and when access was last verified.
- **FR-010**: Notifications for repositories that are not connected in any workspace MUST be
  ignored without fetching repository content. When the same repository is connected in several
  workspaces, each workspace's runs MUST be authorized and stored separately.

**Access checks and revocation**

- **FR-011**: Before fetching any content, every run, automatic or manual, MUST verify that the
  workspace owner can still access the repository on GitHub, and that an installation of the
  CodeAtlas app they can access still covers it (001 FR-003).
- **FR-012**: System MUST check for access loss at the start of every run, when GitHub reports that
  the CodeAtlas app was uninstalled or suspended or that a repository was removed from an
  installation, and in a daily check of every connected repository.
- **FR-013**: Only a definitive answer from GitHub counts as access loss. Examples are a repository
  that is not found, an installation that no longer covers it, and an installation that was
  removed or suspended. Outages, rate limits, and timeouts MUST NOT count as access loss; the check
  MUST be retried later, and the repository's content MUST remain available meanwhile.
- **FR-014**: When access loss is detected, System MUST immediately deny all reads of the
  repository's versions, files, search results, answers, and progress, cancel its queued work,
  and prevent running work from publishing. The repository page MUST show "access lost" and the
  reason.
- **FR-015**: After access loss, System MUST keep the repository's data hidden for a grace period of
  7 days. If a check, a push, or a re-index request confirms during that period that access has
  returned, System MUST restore the repository with its versions and answers, without re-indexing,
  and resume automatic runs. If the period ends without access returning, System MUST disconnect
  the repository and purge its data within 24 hours (001 FR-035).
- **FR-016**: When the workspace owner's GitHub authorization has lapsed and cannot be renewed,
  System MUST pause automatic runs and checks for their repositories, without marking them
  "access lost". The repository page MUST show that signing in again resumes them. After the owner
  signs in, System MUST check access immediately.
- **FR-017**: When a public repository becomes private, automatic runs MUST pause until the owner
  accepts the external processing disclosure (001 FR-006).
- **FR-018**: The daily check MUST also start a run when the default branch head differs from both
  the latest indexed commit and any commit being indexed.

**Retention, audit, and external writes**

- **FR-019**: A superseded version that no unexpired answer references MUST be removed within 24
  hours once 5 newer ready versions of the same repository exist, or after 14 days (001 FR-035),
  whichever comes first.
- **FR-020**: Change notification records MUST be removed after 14 days.
- **FR-021**: System MUST record an audit event for access loss detected, access restored, a
  repository disconnected after access loss, a rejected notification, and automatic updates paused
  or resumed. Each automatic run MUST record what started it.
- **FR-022**: System MUST NOT write to GitHub (001 FR-032) or request write access to repositories.

### Key Entities *(include if feature involves data)*

- **Change notification**: A message from GitHub about a push or an installation change. It has a
  unique delivery identity so repeats can be recognized, a verification outcome, and a record of
  what CodeAtlas did with it.
- **Installation**: An installation of the CodeAtlas app on a GitHub account, which covers a set of
  repositories. Its status is active, suspended, or removed.
- **Repository** (extended): Gains an access state (active, updates paused with a reason, or access
  lost with a reason and detection time), the time access was last verified, and the latest push
  received.
- **Indexing run** (extended): Records what started it (the user, a push, or the daily check) and,
  for a push, the pushed commit.
- **Audit event** (extended): Covers the access and notification events in FR-021.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For repositories up to 100,000 source lines with no other indexing work queued in the
  workspace, 95% of pushes to the default branch become the default version within 5 minutes after
  GitHub delivers the notification.
- **SC-002**: Delivering the same notification 100 times creates at most one run. 20 pushes that
  arrive while a run is in progress create at most one more run, and after any burst of pushes the
  last pushed commit becomes the default version. Pushes that arrive after the previous run has
  finished each start their own run.
- **SC-003**: In fault-injection tests where runs for earlier pushes finish after runs for later
  ones, 100% of outcomes leave the last pushed commit as the default version.
- **SC-004**: 100% of notifications that fail verification are rejected, with no run, fetch, or
  state change.
- **SC-005**: After GitHub reports that the CodeAtlas app was uninstalled or suspended, or that a
  repository was removed from an installation, zero reads of the affected repository's content
  succeed more than 1 minute later.
- **SC-006**: When the owner loses access without any notification, the loss is detected within 24
  hours, and zero reads of the repository's content succeed after detection.
- **SC-007**: Zero runs publish a version after access loss is detected for their repository.
- **SC-008**: In fault-injection tests that make GitHub unavailable or rate-limited during access
  checks, zero repositories are marked "access lost".
- **SC-009**: A push whose notification was never delivered is indexed within 24 hours.
- **SC-010**: No repository keeps more than 5 superseded versions that no unexpired answer
  references for longer than 24 hours.
- **SC-011**: When access returns within 7 days of detection, 100% of the repository's earlier
  versions and answers become readable again without re-indexing. When it does not, the
  repository's data is purged within 8 days of detection.

## Assumptions

- This feature is the second increment of CodeAtlas. It covers the automatic synchronization and
  revocation parts of milestone M2 in `docs/design/codeatlas-v1.md`, and builds on
  `specs/001-repository-qa`.
- Each workspace has one owner, who connected its repositories. Automatic runs and checks act on
  the owner's behalf and verify the owner's access.
- Automatic re-indexing is always on for connected repositories. A per-repository switch to turn it
  off is out of scope.
- Automatic runs follow the default branch only. A new default-branch version becomes the default
  view even if the user last indexed another branch by hand; other branches can still be indexed
  manually.
- Each run indexes the full commit. Reusing unchanged files from an earlier version (incremental
  indexing) is out of scope, because a full index of a repository of about 97,000 source lines took
  about 4 seconds in the first increment.
- GitHub does not automatically redeliver notifications that CodeAtlas failed to receive; the daily
  check covers them.
- Users are informed only in the CodeAtlas interface. Email or other notifications are out of
  scope.
- Pull request branches, issue events, and CI events are out of scope; later features handle them.
- The numeric limits in this spec are starting values and remain configurable.
