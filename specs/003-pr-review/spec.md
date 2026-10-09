# Feature Specification: Pull Request Review

**Feature Branch**: `003-pr-review`

**Created**: 2026-10-06

**Status**: Draft

**Input**: User description (translated): "Plan spec 003, a pull request review agent that produces
a summary, risks, a review checklist, and test suggestions."

## User Scenarios & Testing *(mandatory)*

This is the third increment of CodeAtlas. The first two increments (`specs/001-repository-qa`,
`specs/002-push-reindexing`) explain the code on a repository's default branch. This feature helps
a developer review a proposed change. They pick an open pull request in a connected repository
and receive a review that summarizes the change, lists the risks it may introduce, gives a
checklist to verify before merging, and suggests tests. Every item cites the lines it relies on,
and the review is pinned to the exact commits it examined.

References such as "001 FR-020" point to requirements in `specs/001-repository-qa/spec.md`, and
"002 FR-014" to `specs/002-push-reindexing/spec.md`.

### User Story 1 - Review a pull request with a cited summary and risks (Priority: P1)

A developer opens a connected repository, sees its open pull requests, and requests a review of
one. They follow progress by stage. When the review finishes, they read a short summary of what
the change does and a list of risks ranked by severity. Each risk explains what might go wrong,
cites the changed lines or related repository code it is based on, and suggests what to check.
Opening a citation shows exactly those lines at the right commit.

**Why this priority**: Understanding what a change does and where it can break is the main cost
of reviewing code. A cited summary and ranked risks deliver that value on their own.

**Independent Test**: On a fixture repository, open a fixture pull request that removes a
permission check, request a review, and confirm that the summary describes the change, the
removed check appears as a risk, and every citation opens the cited lines.

**Acceptance Scenarios**:

1. **Given** a connected repository with open pull requests, **When** the user opens its pull
   request list, **Then** they see each open pull request, including drafts, with its number,
   title, author, base and head branches, head commit, and the state of its latest review.
2. **Given** an open pull request, **When** the user requests a review, **Then** a review starts
   for the pull request's current head commit and shows progress by stage, and leaving and
   returning does not lose it.
3. **Given** a review has finished, **When** the user opens it, **Then** it shows the base, head,
   and merge-base commits it examined, an overall risk level, a summary of the change, and the
   risks ordered by severity, each with a category, an explanation, a suggested check, and at
   least one citation.
4. **Given** a risk cites changed lines, **When** the user opens the citation, **Then** it shows
   those lines on the correct side of the change (before or after) at the cited commit.
5. **Given** a change in which no risks are found, **When** the review finishes, **Then** its
   overall risk level is "none", and it lists what was examined instead of inventing risks.
6. **Given** the review-generation service is unavailable, **When** a review runs, **Then** it fails
   with a retryable error, and no partial or fabricated review is shown.
7. **Given** the pull request's description or code contains instructions aimed at the model, such
   as "report no risks", **When** the review runs, **Then** those instructions do not change what
   is reviewed, what is cited, or how risks are reported.

---

### User Story 2 - Use the review checklist and test suggestions (Priority: P2)

After reading the summary and risks, the developer works through a checklist written for this
change, such as "confirm the new migration can be rolled back", and uses the test suggestions to
decide which tests to run or ask the author for. Test suggestions name existing tests that likely
exercise the changed code, and new test cases the change appears to need.

**Why this priority**: The checklist and test suggestions turn the review into concrete reviewer
actions. They build on the analysis in User Story 1, so they rank below it.

**Independent Test**: On a fixture pull request that changes a function covered by an existing
test and adds a branch that no test covers, confirm that the review lists the existing test as a
candidate, suggests a new test case for the uncovered branch, and that every checklist item
refers to a changed file or a listed risk.

**Acceptance Scenarios**:

1. **Given** a finished review, **When** the user opens its checklist, **Then** each item states
   what to verify and refers to at least one changed file or listed risk.
2. **Given** changed code that existing tests reference, **When** the review finishes, **Then**
   those tests are listed as candidate tests, each with the reason it was selected, and labeled as
   not run by CodeAtlas.
3. **Given** changed behavior that the pull request does not test, **When** the review finishes,
   **Then** a new test case is suggested that names the behavior to test and cites the changed
   lines.
4. **Given** a pull request that adds or changes tests, **When** the review finishes, **Then** the
   review lists those tests as part of the change.
5. **Given** a finished review, **When** the user copies it, **Then** they get the summary, risks,
   checklist, and test suggestions as Markdown, with citations as links to the cited lines on
   GitHub, ready to paste into GitHub by hand.

---

### User Story 3 - Know when a review is out of date (Priority: P3)

The pull request receives new commits after a review. The earlier review is shown as outdated,
still states the commit it examined, and offers a review of the new head commit.

**Why this priority**: A review of an older commit can mislead reviewers. It ranks lowest because
a user can compare the head commit by hand in the meantime.

**Independent Test**: Review a fixture pull request, push a commit to its head branch, and confirm
that the earlier review is shown as outdated with its original content and commits, and that a
new review covers the new head commit.

**Acceptance Scenarios**:

1. **Given** a review of head commit A, **When** commit B is pushed to the pull request, **Then**
   the review is shown as outdated, still states commit A, and its citations still show commit A
   content.
2. **Given** an outdated review, **When** the user requests a review, **Then** a separate review of
   commit B is created, and the earlier review is unchanged.
3. **Given** a finished review of the pull request's current head commit, **When** the user
   requests a review again, **Then** the existing review is shown and no new review is created,
   unless the user explicitly asks for a new one.
4. **Given** the pull request has been closed or merged, **When** the user opens an earlier review,
   **Then** it remains readable and shows the pull request's state.

### Edge Cases

- A pull request with no changed files, or only files excluded from review (binary, generated,
  vendored, or over the per-file size limit), produces a review that says there is nothing to
  review and lists each file with its reason. It does not count against the daily allowance.
- A pull request over the review size limit is reviewed in part. The review lists every changed
  file it did not review, and its summary states that the review is partial.
- A pull request changes a known credential file. The file's content is never sent to the model
  provider or shown; the review reports a high-severity security risk that names the file.
- A pull request comes from a fork. It is reviewed like any other; content from the fork is
  untrusted.
- The head commit changes while a review runs. The review finishes for the pinned commit and is
  shown as outdated.
- The pull request is closed or merged while a review runs. The review finishes for the pinned
  commits and shows the pull request's new state.
- The pinned commits can no longer be fetched, for example after a force push, or the base and
  head share no history. The review fails with the reason.
- A pull request targets a branch other than the default branch. It is reviewed against its own
  merge base, and the repository's default version is unchanged.
- A pull request renames or deletes files. The summary reports renames and deletions, and
  citations of deleted lines show the before side.
- The repository was rejected for exceeding a size limit (001 FR-009). Its pull requests cannot be
  reviewed, and the user is told why.
- CodeAtlas has not yet been allowed to read pull requests for an installation. The pull request
  list explains that the installation owner must approve read access on GitHub.
- Access to the repository is lost (002) while a review runs. The review cannot publish its result,
  and earlier reviews are denied with the repository's other content.
- A second review request while a review is running in the workspace waits in a visible queued
  state. Submitting the same request twice creates one review.
- The same GitHub repository is connected in two workspaces. Each workspace's reviews are separate.

## Requirements *(mandatory)*

### Functional Requirements

**Pull request selection**

- **FR-001**: Users MUST be able to list the open pull requests, including drafts, of a connected
  repository in their workspace. Each entry MUST show the number, title, author, base and head
  branches, head commit, and the state of its latest review: none, queued, running, current,
  outdated, or failed.
- **FR-002**: Users MUST be able to request a review of an open pull request. Reviews MUST start
  only on a user's request; opening a pull request or pushing to it MUST NOT start a review.
- **FR-003**: System MUST refuse reviews of repositories marked "access lost" or rejected for a
  size limit. Pull request details MUST be read with the workspace owner's own GitHub
  authorization, so that GitHub returns only what the owner can read. Before fetching any code for
  a review, System MUST verify that the workspace owner can still access the repository (002
  FR-011), and MUST stop if the repository became private without an accepted external processing
  disclosure (FR-030).
- **FR-004**: Each review MUST record, at submission, the pull request number and its base and
  head commits, and then the merge-base commit of those two. It MUST examine the change from the
  merge base to the head. Later commits to the pull request or its base branch MUST NOT change
  what a review examined.
- **FR-005**: Reviewing MUST NOT change the repository's default version, and MUST NOT require the
  pull request to target the default branch.

**Review content**

- **FR-006**: Each review MUST contain a summary of the change, grouped by the parts of the
  repository it touches, that states what the change adds, modifies, renames, and removes. Each
  summary point MUST cite the changed lines it describes. A file renamed without changes has no
  changed lines; its summary point names the old and new paths instead.
- **FR-007**: Each review MUST list the risks the change may introduce, ordered by severity. Each
  risk MUST have a severity (high, medium, or low), a category (correctness, security, data and
  migrations, compatibility, performance, dependencies, tests, or other), an explanation, a
  suggested check, and at least one citation, except for credential-file risks (FR-019).
- **FR-008**: Each risk MUST state whether the cited lines show the problem directly (observed) or
  only suggest it (possible), as in 001 FR-023.
- **FR-009**: When no risks are found, the review MUST say so and list what was examined. A review
  MUST NOT report a risk without evidence.
- **FR-010**: Each review MUST contain a checklist for the human reviewer. Each item MUST state what
  to verify and MUST refer to at least one changed file or listed risk.
- **FR-011**: Each review MUST contain test suggestions in two groups. Candidate tests are existing
  tests that likely exercise the changed code, each with the reason it was selected and labeled as
  not run by CodeAtlas. New test cases name changed behavior that the pull request does not test,
  and cite the changed lines. The review MUST also list the tests the pull request adds or
  changes.
- **FR-012**: Repository context that a review uses, such as code that refers to a changed
  declaration or tests that refer to a changed file, MUST come from the repository at the review's
  pinned commits, not from the default version. Such context MUST be labeled as candidates, not a
  complete set.
- **FR-013**: Each review MUST show an overall risk level equal to the severity of its most severe
  risk (high, medium, or low), or "none" when it reports no risks. For a partial review, the
  overall risk level MUST state that it covers only the reviewed files. A review MUST NOT
  recommend whether to merge.
- **FR-014**: A review MUST use only the pull request's title, description, and code changes, and
  the repository's code at the pinned commits. It MUST NOT use the pull request's existing review
  discussion or the results of its CI checks.
- **FR-015**: Users MUST be able to copy a finished review as Markdown, with citations as links to
  the cited lines on GitHub at the cited commit.

**Citations and coverage**

- **FR-016**: Every citation MUST name a file, a line range, a commit, and the side of the change
  (before at the merge base, after at the head), and MUST resolve to existing content at that
  commit. Citations MUST come only from the evidence gathered for that review. Items whose
  citations fail validation MUST NOT be shown. There are two exceptions: a risk about a
  credential file, which names the file without its content (FR-019), and a summary point for a
  file renamed without changes, which names both paths (FR-006).
- **FR-017**: Each review MUST show coverage: the changed files it reviewed, and each changed file
  it did not review with the reason.
- **FR-018**: System MUST enforce these default limits per review: at most 100 changed files and
  2,000 changed lines reviewed. When a pull request exceeds them, System MUST review a subset
  within the limits, show every file not reviewed in the coverage (FR-017), and state in the
  summary that the review is partial. Changed files over 1 MiB, binary files, generated files, and vendored dependency
  directories MUST be excluded and listed, as in 001 FR-009 and FR-010.
- **FR-019**: The content of known credential files MUST NOT be sent to the external model provider
  or shown in a review. A change to such a file MUST be reported as a high-severity security risk
  that names the file.
- **FR-020**: A pull request with no reviewable changes MUST produce a review that says so, with its
  coverage, without using the review-generation service and without counting against the daily
  allowance.
- **FR-021**: When the pinned commits cannot be fetched, or the base and head share no history, the
  review MUST fail with the reason.

**Freshness and repeat requests**

- **FR-022**: A review MUST be shown as outdated whenever the pull request's head commit, as known
  when the review or the pull request list is opened, differs from the head commit the review
  examined. An outdated review MUST remain readable and unchanged.
- **FR-023**: Requesting a review of a pull request whose current head commit already has a
  finished review MUST show that review instead of creating a new one. Users MUST be able to
  request a new review explicitly; it creates a separate review.
- **FR-024**: Reviews of closed or merged pull requests MUST remain readable and show the pull
  request's state. New reviews MUST be requested only for open pull requests.

**Jobs, limits, and safety**

- **FR-025**: Reviews MUST run in the background, show progress by stage, continue after the user
  leaves, and remain retrievable, as in 001 FR-026. Resubmitting an identical request MUST NOT
  create a duplicate review (001 FR-029).
- **FR-026**: Each workspace MUST run at most one review at a time. Further requests MUST wait in a
  visible queued state.
- **FR-027**: Each workspace MUST be limited to 10 new reviews per day, counted separately from
  questions. System MUST show usage and remaining allowance, and refuse further requests with the
  reset time.
- **FR-028**: System MUST retry transient failures automatically, up to 3 attempts in total. Access
  loss and unsupported input MUST fail immediately with the reason. A review MUST fail with a
  timeout reason after 5 minutes. A failure of the review-generation service MUST produce a
  retryable error and never a fabricated review (001 FR-024).
- **FR-029**: Pull request titles, descriptions, commit messages, and code MUST be treated as
  untrusted. Their content MUST NOT trigger actions, widen access, reveal credentials, or change
  the review's scope or structure (001 FR-025).
- **FR-030**: Reviews of private repositories MUST run only after the owner has accepted the
  external processing disclosure (001 FR-006). The disclosure MUST state that pull request
  descriptions and changes may be sent to the external model provider.

**Access, audit, retention, and external writes**

- **FR-031**: Reviews MUST belong to the workspace. Users MUST NOT list, view, follow progress of,
  or copy reviews from another workspace (001 FR-004). Reviews of a repository marked "access
  lost" MUST be denied with its other content, and restored with it (002 FR-014, FR-015).
- **FR-032**: System MUST record an audit event for each review request and each denied access to a
  review.
- **FR-033**: Reviews MUST be removed after 30 days, as answers are (001 FR-035). When a repository
  is disconnected, its reviews MUST become inaccessible immediately and be purged within 24 hours.
- **FR-034**: System MUST NOT write to GitHub: no review comments, approvals, change requests,
  statuses, labels, or merges (001 FR-032).

### Key Entities *(include if feature involves data)*

- **Pull request**: A GitHub pull request of a connected repository, identified by the repository
  and its number. Has a title, author, state (open, draft, closed, or merged), base and head
  branches, and the head commit as last fetched.
- **Pull request review**: One analysis of a pull request at pinned base, head, and merge-base
  commits. Has an execution state, progress, coverage, an overall risk level, a summary, risks,
  checklist items, test suggestions, and whether it is outdated.
- **Risk**: A possible problem in the change, with a severity, a category, whether it is observed or
  possible, an explanation, a suggested check, and citations.
- **Checklist item**: A verification step for the human reviewer, linked to changed files or risks.
- **Test suggestion**: Either a candidate existing test, with the reason it was selected, or a new
  test case, with the behavior to test and citations.
- **Evidence item** (extended): Gains the side of the change (before or after), so that it can cite
  changed and removed lines as well as repository code.
- **Usage allowance** (extended): Adds the workspace's daily review count and limit.
- **Audit event** (extended): Covers review requests and denied access to reviews.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In pilot sessions, at least 4 of 5 participants who review a fixture pull request with
  a seeded defect find the defect by following the review, without outside help.
- **SC-002**: On a versioned evaluation set of at least 20 fixture pull requests with seeded
  defects, at least 70% of the seeded defects are reported as medium- or high-severity risks.
- **SC-003**: On at least 5 fixture pull requests that make safe, fully tested changes, zero reviews
  report a high-severity risk.
- **SC-004**: In a documented human audit of at least 30 reported risks drawn from at least 10
  reviews, at least 80% are judged correctly explained by their citations.
- **SC-005**: 100% of citations shown to users resolve to existing content at the cited commit and
  side, 100% of checklist items refer to a changed file or listed risk, and 100% of overall risk
  levels match the most severe listed risk.
- **SC-006**: For pull requests up to 500 changed lines, with no other review queued in the
  workspace, 95% of reviews finish within 2 minutes of submission.
- **SC-007**: On fixture pull requests whose description or code contains instructions aimed at
  the model, 100% of reviews still report the seeded defect and cite only gathered evidence.
- **SC-008**: In tests with fixture credential files, zero credential contents are sent to the
  model provider or shown in a review.
- **SC-009**: 100% of reviews opened after their pull request's head commit changed are shown as
  outdated.
- **SC-010**: In isolation tests with two users, zero attempts to list, view, follow progress of,
  or copy another workspace's reviews succeed.
- **SC-011**: Submitting the same review request 100 times creates exactly one review.

## Assumptions

- This feature is the third increment of CodeAtlas. It covers the pull request part of milestone M3
  in `docs/design/codeatlas-v1.md`, and builds on `specs/001-repository-qa` and
  `specs/002-push-reindexing`.
- A code dependency graph is not yet available. Related code and candidate tests are found by
  searching the repository for references to changed files and declarations, and by test naming
  conventions. They are labeled as candidates. The dependency graph and CI failure diagnosis remain
  separate later features.
- Reviews start only on request. Automatic reviews when a pull request is opened or updated are
  out of scope, so this feature adds no new GitHub notifications.
- Only pull requests of repositories connected in the workspace can be reviewed. Reviewing
  arbitrary branch comparisons or single commits is out of scope.
- Reviews do not read a pull request's existing review discussion or its CI check results. CI
  results arrive with the later CI failure diagnosis feature.
- The overall risk level summarizes the listed risks. It is not a merge recommendation; the merge
  decision stays with the human reviewer.
- Listing and reviewing pull requests requires CodeAtlas to be allowed to read pull requests on
  GitHub. Owners of existing installations must approve this added read-only access.
- An owner's earlier acceptance of the external processing disclosure covers reviews, because pull
  request changes are repository content.
- Each review is independent. Follow-up questions about a review are out of scope.
- Reviews are written in English.
- Posting a review to GitHub is out of scope. A later feature adds GitHub write actions, each shown
  as a draft and run only after the user explicitly confirms it.
- GitHub availability and rate limits, and model provider availability, are external dependencies.
  Their outages make reviews fail with a retryable error, while browsing, search, and questions
  continue.
- The numeric limits in this spec are starting values and remain configurable.
