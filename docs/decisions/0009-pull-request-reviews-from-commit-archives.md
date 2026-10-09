# ADR 0009: Pull request reviews read commit archives instead of indexing them

- **Status**: Accepted
- **Date**: 2026-10-06
- **Scope**: `specs/003-pr-review`

## Context

A pull request review examines the change from the merge base to the head. It needs the changed
files on both sides, declarations in those files, related code, and candidate tests at the head.
Its citations must show exact lines at the right commit.

The design document proposes indexed snapshots of the pull request's before and after commits that
never become the default version. Today a snapshot always becomes the default when it is published
or reused. Unreferenced snapshots are capped at 5 per repository. A snapshot of a 100,000-line
repository stores its files, chunks, symbols, and trigram indexes.

## Options considered

1. Index the merge-base and head commits as pull request snapshots.
2. Read GitHub's patch text only (from comparing commits, or from listing pull request files).
3. In the review job, download the merge-base and head archives, filter them with the indexing
   filters, diff and search them in memory, and store only cited excerpts.

## Decision

Option 3:

- The job reads both archives with the existing extraction and filters, and the head tree with the
  existing limits. It diffs changed text files with `difflib`, parses changed files with the
  existing tree-sitter parser, and searches the head tree for related code and tests.
- Each review stores its evidence excerpts, with the side and commit, and its result. It does not
  store a snapshot.
- Reviews are `analysis_runs` rows of kind `pull_request_review`. `snapshot_id` and `question`
  become nullable, with checks by kind.

## Trade-off

- **Gained**:
  - Storage stays at a few hundred kilobytes per review instead of a full index.
  - Snapshot publishing, reuse, version lists, and retention are unchanged.
  - Diffs are exact, with no truncated or omitted patches.
- **Accepted**:
  - Citations show stored excerpts and a GitHub link, not CodeAtlas's file browser.
  - Each review downloads its archives again.
  - Related code is found by text search, not by an import graph.
  - The worker holds the head's eligible files in memory, within the existing 100 MiB limit.
- **Revisit when**:
  - users need to browse or search a pull request's code inside CodeAtlas;
  - the dependency graph feature needs indexed pull request commits; or
  - archive downloads become a measured share of review latency.

Details: `specs/003-pr-review/research.md` R3 to R6.
