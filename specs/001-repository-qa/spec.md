# Feature Specification: Repository Q&A with Citations

**Feature Branch**: None created (spec directory: `specs/001-repository-qa`)

**Created**: 2026-10-03

**Status**: Draft

**Input**: User description (translated and summarized): "Rescope the first CodeAtlas specification
to the first incremental release, repository Q&A (milestone M1 in
`docs/design/codeatlas-v1.md`). Later capabilities are specified as separate features."

## User Scenarios & Testing *(mandatory)*

This is the first increment of CodeAtlas. A developer signs in, connects a GitHub repository,
waits for it to be indexed, asks where something is implemented, and opens the cited code. Every
answer is tied to the exact commit it describes and cites the evidence it relies on.

### User Story 1 - Connect a repository and track indexing (Priority: P1)

A developer signs in with their GitHub account, chooses a repository they have allowed CodeAtlas
to read, and starts indexing. They watch progress by stage. When indexing finishes, they see which
commit was indexed and what was covered or skipped.

**Why this priority**: Every other capability needs an indexed version of a repository. This is
the entry point of the product.

**Independent Test**: Sign in, connect a small fixture repository, and confirm that it reaches a
ready state showing the indexed commit and a coverage summary.

**Acceptance Scenarios**:

1. **Given** a signed-in user who has allowed CodeAtlas to read repository X, **When** they connect
   X, **Then** indexing starts for the latest commit of X's default branch and progress is shown
   by stage.
2. **Given** indexing is in progress, **When** the user closes the browser and returns later,
   **Then** indexing has continued and the current progress or final outcome is shown.
3. **Given** indexing has finished, **When** the user opens the repository, **Then** they see the
   indexed commit, the completion time, the number of indexed files, and each skipped file with
   its reason.
4. **Given** a repository that exceeds a size limit, **When** the user connects it, **Then** indexing is
   rejected with a message naming the limit that was exceeded, and the repository is shown as
   rejected until the user disconnects it.
5. **Given** a repository the user has not allowed CodeAtlas to read, or cannot access on GitHub,
   **When** they try to connect it, **Then** the connection is refused and no repository content
   is fetched.
6. **Given** a private repository, **When** the user connects it, **Then** they are told that
   selected source excerpts may be sent to an external model provider, and indexing starts only
   after they accept.
7. **Given** a ready indexed version exists, **When** the user re-indexes and the attempt fails,
   **Then** the previous ready version remains available and the failure reason is shown.

---

### User Story 2 - Ask a question and verify the answer through citations (Priority: P1)

A developer asks a natural-language question about an indexed repository, such as "Where are
repository permissions checked?". They receive an answer that cites specific files and line
ranges at the indexed commit. Opening a citation shows exactly those lines. When the repository
does not contain enough evidence, CodeAtlas says so instead of guessing.

**Why this priority**: Trustworthy, verifiable answers are the core value of the product.

**Independent Test**: On a pre-indexed fixture repository, ask one answerable question and confirm
every citation opens the cited lines; ask one unanswerable question and confirm the result is
"insufficient evidence".

**Acceptance Scenarios**:

1. **Given** a ready indexed repository, **When** the user asks an answerable question, **Then**
   the answer cites file paths and line ranges at the indexed commit, and each citation opens the
   cited lines.
2. **Given** a question the repository cannot answer, **When** the user asks it, **Then** the
   result states that evidence is insufficient and names what is missing.
3. **Given** the answer-generation service is unavailable, **When** the user asks a question,
   **Then** they receive a retryable error rather than an answer, and browsing and search still
   work.
4. **Given** an answer was produced for commit A and the repository was later re-indexed at commit
   B, **When** the user reopens the earlier answer, **Then** it still states commit A and its
   citations show commit A content.
5. **Given** an answer is being prepared, **When** the user watches the request, **Then** they see
   its progress by stage, and leaving and returning does not lose the result.
6. **Given** the workspace has used its daily question allowance, **When** the user submits a
   question, **Then** the request is refused with the limit and the time it resets.

---

### User Story 3 - Browse and search code at the indexed commit (Priority: P2)

A developer explores an indexed repository: they browse the file tree, open files, search by
text, path, or symbol name, and search the documentation.

**Why this priority**: Lets users orient themselves and inspect cited code in context. Q&A
delivers value first; this story makes the evidence easier to explore.

**Independent Test**: On a pre-indexed fixture repository, find a known symbol by name and open
its file at the declared line.

**Acceptance Scenarios**:

1. **Given** a ready indexed repository, **When** the user opens the file tree, **Then** they see
   the files at the indexed commit and can open any file at a chosen line range.
2. **Given** a ready indexed repository, **When** the user searches for a symbol or path name,
   **Then** exact symbol and path matches appear before other matches, each with file and line.
3. **Given** a ready indexed repository, **When** the user searches the documentation in natural
   language, **Then** relevant documentation passages appear with their source location.
4. **Given** meaning-based documentation search is unavailable, **When** the user searches the
   documentation, **Then** keyword results are returned with a visible notice that search is
   running in a reduced mode.
5. **Given** several ready indexed versions of a repository, **When** the user selects an older
   one, **Then** browsing and search reflect that version's commit.

---

### User Story 4 - Disconnect a repository and remove its data (Priority: P3)

A developer disconnects a repository. Its data becomes inaccessible at once and is removed within
a day.

**Why this priority**: Users connecting private code need a reliable way to withdraw it, but the
first demonstration does not depend on it.

**Independent Test**: Disconnect a fixture repository, confirm that every page and search for it
is denied immediately, and confirm its stored data is gone after the purge window.

**Acceptance Scenarios**:

1. **Given** a connected repository with queued and running work, **When** the user disconnects
   it, **Then** all its data is immediately inaccessible, queued work is canceled, and running
   work cannot publish results.
2. **Given** a disconnected repository, **When** 24 hours have passed, **Then** its indexed content
   and derived data have been purged.
3. **Given** a previously disconnected repository, **When** the user connects it again, **Then** it
   is indexed from scratch.

### Edge Cases

- A repository with no supported-language files is indexed for text and documentation search, and
  coverage states that no code structure was extracted.
- An empty repository, or one whose default branch has no commits, cannot be indexed; the user is
  told why.
- Files over 1 MiB, binary files, generated files, vendored dependency directories, symbolic links,
  and known credential files are excluded and listed in coverage.
- Repository entries whose names traverse outside the repository, or links that point outside it,
  are rejected, and the rejection is recorded.
- A repository renamed or transferred on GitHub remains the same connected repository.
- Two users connecting the same upstream repository get fully separate data.
- Files deleted or renamed between two indexed commits never appear under their old paths in the
  newer version.
- Repository text that contains instructions aimed at the model does not change what CodeAtlas
  does, what it can access, or what it cites.
- A generated answer that cites a location not present in the gathered evidence is rejected, never
  shown.
- A second indexing request, or a second question, while one of the same kind is running in the
  workspace waits in a visible queued state.
- Submitting the same request twice (double-click or network retry) creates one job.
- A user's session expires during a long job; the job continues and the result is available after
  signing in again.

## Requirements *(mandatory)*

### Functional Requirements

**Identity and access**

- **FR-001**: Users MUST sign in with a GitHub account; CodeAtlas MUST NOT offer anonymous use.
- **FR-002**: Each user MUST have a personal workspace that owns all of their repositories,
  indexed versions, and answers.
- **FR-003**: System MUST connect or index a repository only after verifying that the signed-in
  user can access it on GitHub and has allowed CodeAtlas to read it.
- **FR-004**: Users MUST NOT be able to list, search, view, follow progress of, or download data
  from another workspace; inaccessible resources MUST look the same as nonexistent ones.
- **FR-005**: Sessions MUST expire, and signing out MUST invalidate the session immediately.

**Repository connection and indexing**

- **FR-006**: Users MUST be able to connect public and private GitHub repositories. Before a
  private repository is indexed, System MUST disclose that selected excerpts may be sent to an
  external model provider and require the user's acceptance.
- **FR-007**: System MUST index the latest commit of the default branch, or of a branch the user
  selects, and record the exact commit indexed.
- **FR-008**: System MUST show indexing progress by stage and the final outcome (ready, or failed
  with a reason), and this status MUST remain available after the user leaves and returns.
- **FR-009**: System MUST enforce these default limits: 10 repositories per workspace; per indexed
  version, 5,000 eligible files, 100,000 source lines, and 100 MiB of expanded source; 1 MiB per
  text file. Repositories over a version limit MUST be rejected with the limit named; files over
  the per-file limit MUST be skipped and reported.
- **FR-010**: System MUST exclude binary files, generated files, vendored dependency directories,
  symbolic links, and known credential files, and record each exclusion in coverage.
- **FR-011**: System MUST treat repository content as data only: it MUST NOT execute repository
  code, hooks, build scripts, or tests, and MUST NOT install repository dependencies.
- **FR-012**: An indexed version MUST become usable only when complete, and MUST contain only files
  present at its commit. A failed attempt MUST leave the previous ready version as the default.
- **FR-013**: System MUST extract declarations from Python and TypeScript files. Other text files
  MUST be searchable as text. Coverage MUST report files with unsupported syntax.
- **FR-014**: Users MUST be able to re-index a connected repository on demand.

**Browse and search**

- **FR-015**: Users MUST be able to browse the file tree and view bounded line ranges of files in
  any ready version of a repository in their workspace.
- **FR-016**: Users MUST be able to search source by text, path, and symbol name, with exact path
  and symbol matches ranked first.
- **FR-017**: Users MUST be able to search Markdown documentation by meaning and by keyword. When
  meaning-based search is unavailable, System MUST return keyword results and show that search is
  running in a reduced mode.
- **FR-018**: Search and browse results MUST come only from the selected version of a repository in
  the user's workspace.

**Repository Q&A**

- **FR-019**: Users MUST be able to ask a natural-language question about a ready version of a
  repository. Each question is answered independently, without memory of earlier questions.
- **FR-020**: Each answer MUST cite its evidence as file path and line range at the pinned commit.
  Every displayed citation MUST resolve to existing content, and answers MUST NOT cite locations
  outside the evidence gathered for that question.
- **FR-021**: When evidence is insufficient, System MUST report "insufficient evidence" and the
  evidence gaps instead of an unsupported answer.
- **FR-022**: Every answer MUST record the commit and indexed version used at submission; later
  indexing MUST NOT change them.
- **FR-023**: Answers MUST distinguish observed facts from possible explanations.
- **FR-024**: A failure of the answer-generation service MUST produce a retryable error, distinct
  from "insufficient evidence", and never a fabricated answer.
- **FR-025**: Repository text MUST be treated as untrusted. Its content MUST NOT trigger actions,
  widen access, or reveal credentials.

**Jobs, limits, and usage**

- **FR-026**: Indexing and question answering MUST continue after the user leaves, and their
  progress and results MUST remain retrievable; a reconnecting user MUST receive progress they
  missed.
- **FR-027**: Each workspace MUST run at most one indexing job and one question at a time. Further
  requests MUST wait in a visible queued state.
- **FR-028**: Each workspace MUST be limited to 20 new questions per day. System MUST show usage
  and remaining allowance, and refuse further requests with the reset time.
- **FR-029**: Resubmitting an identical request MUST NOT create a duplicate job.
- **FR-030**: System MUST retry transient failures automatically, up to 3 attempts in total. Access
  loss, unsupported input, and size limits MUST fail immediately with the reason.
- **FR-031**: Indexing MUST fail with a timeout reason after 15 minutes, and a question after 3
  minutes.

**External writes, audit, and data removal**

- **FR-032**: This feature MUST NOT write to GitHub: no comments, statuses, commits, merges, or
  settings changes.
- **FR-033**: System MUST record an audit event for sign-in, repository connection and
  disconnection, question submission, and access denial.
- **FR-034**: Disconnecting a repository MUST immediately deny all access to its data, cancel its
  queued work, and prevent running work from publishing results.
- **FR-035**: System MUST purge a disconnected repository's derived data within 24 hours, and by
  default remove unreferenced indexed versions after 14 days and answers after 30 days. Data
  referenced by an unexpired answer MUST be kept until that answer expires.

### Key Entities *(include if feature involves data)*

- **User**: A person identified by their GitHub account.
- **Workspace**: The ownership and access boundary. Each user has one personal workspace.
- **Repository**: A GitHub repository connected to a workspace, identified by its GitHub identity
  so that renames do not duplicate it. Has a default indexed version.
- **Indexed version (snapshot)**: The repository at one commit, processed with one indexing
  configuration. Has a status, a coverage summary, and its files, symbols, and documentation
  passages.
- **File and Symbol**: The contents of an indexed version. Symbols are declarations with a
  location.
- **Documentation passage**: A section of Markdown documentation in an indexed version, searchable
  by meaning and keyword.
- **Question and answer**: A question about one indexed version, with its pinned inputs, execution
  state, quality outcome (for example, insufficient evidence), and answer.
- **Evidence item**: An exact excerpt an answer relies on, with its file, commit, and line range.
- **Job and progress event**: Background work for indexing or answering, with an ordered list of
  progress events the user can follow.
- **Usage allowance**: The workspace's daily question count and limit.
- **Audit event**: A record of who did what to which resource, when, and with what outcome.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In pilot sessions, at least 4 of 5 participants who connect a repository locate where
  a named feature is implemented by following CodeAtlas citations, without outside help.
- **SC-002**: Indexing completes within 15 minutes for repositories at the maximum supported size
  of 100,000 source lines.
- **SC-003**: 100% of citations shown to users resolve to existing content at the cited commit.
- **SC-004**: On a versioned evaluation set of at least 50 questions, at least 80% of the labeled
  relevant evidence for answerable questions appears among the top 5 retrieved items, on average.
- **SC-005**: In a documented human audit, at least 90% of factual claims in sampled answers are
  supported by their cited evidence.
- **SC-006**: At least 80% of held-out unanswerable questions receive an "insufficient evidence"
  result.
- **SC-007**: With 10 concurrent users, 95% of searches return within 1.5 seconds, 95% of
  views of connected repositories, indexed versions, and files load within 0.5 seconds, and 95% of
  submissions are acknowledged within 1 second, excluding answer-generation time. The list of
  GitHub repositories available to connect depends on GitHub and is excluded.
- **SC-008**: In isolation tests with two users, zero attempts to list, search, view, follow
  progress of, or download another workspace's data succeed.
- **SC-009**: Submitting the same request 100 times creates exactly one job.
- **SC-010**: In fault-injection tests that interrupt indexing or answering, 100% of jobs either
  complete with no duplicate or missing results or fail with a stated reason.
- **SC-011**: After disconnection, zero reads of the repository's data succeed, and its derived data
  is purged within 24 hours.

## Assumptions

- This feature is the first increment of CodeAtlas and corresponds to milestone M1 in
  `docs/design/codeatlas-v1.md`, plus repository disconnection.
- The pilot serves a small number of users. Each user has one personal workspace; invitations and
  shared team workspaces are out of scope.
- GitHub is the only supported provider. Repositories are chosen from those the user has allowed
  CodeAtlas to read, not from arbitrary URLs or local paths.
- The user interface is in English.
- The numeric limits in this spec are starting values and remain configurable.
- The external model services used for answers and meaning-based search are chosen during
  planning. Private repositories are available only to pilot users who accept the external
  processing disclosure.
- GitHub availability and rate limits, and model provider availability, are external dependencies.
  Their outages degrade question answering but not browsing and search of ready versions.
- Known limitation: if a user loses access to a repository on GitHub, CodeAtlas detects it the next
  time the repository is connected or indexed. Continuous access rechecks arrive with automatic
  re-indexing in a later feature.
- Later features, each specified separately, in rough order: automatic re-indexing on push with
  access revocation handling; a code dependency graph; pull request impact reports; CI failure
  diagnosis; issue sync and triage; weekly engineering reports; knowledge-base document uploads;
  GitHub write actions through reviewed drafts, including review comments and merge suggestions; a
  conversational assistant with memory; multi-agent workflows; an answer-quality evaluation
  dashboard; and pilot deployment with operational readiness.
- Out of scope until further notice: Git providers other than GitHub, and executing repository
  code.
