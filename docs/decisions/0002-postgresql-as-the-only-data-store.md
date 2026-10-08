# ADR 0002: PostgreSQL as the only data store

- **Status**: Accepted
- **Date**: 2026-10-03
- **Scope**: `specs/001-repository-qa`; revisited for `specs/004-pilot-deployment` and kept
  (ADR 0010)

## Context

Indexing stores the eligible files of each repository version: up to 5,000 files and 100 MiB per
version, 10 repositories per workspace. Several kinds of data must live somewhere:

- relational records;
- substring, path, and symbol search;
- ranked full-text search;
- documentation embeddings.

The design document proposed PostgreSQL with pgvector for records and vectors, and S3 for source
bundles.

## Options considered

1. PostgreSQL only, with pgvector, pg_trgm, and built-in full-text search. File contents are
   stored in a table.
2. PostgreSQL for records and vectors, with S3 for file contents (the design document).
3. PostgreSQL plus a search engine (OpenSearch or Zoekt), with or without S3.

## Decision

Option 1. Vector search is exact and filtered by snapshot, with no approximate index.

## Trade-off

- **Gained**:
  - One stateful service in development, CI, and production.
  - File contents and the rows that reference them commit atomically, so there is no
    upload-before-commit protocol and no orphan cleanup.
  - Authorization filters live in the same SQL query as the search.
- **Accepted**:
  - A larger database and longer backups.
  - Trigram and full-text indexes are less capable than a dedicated code-search engine.
- **Revisit when**:
  - database size or backup time becomes a measured problem; or
  - search p95 exceeds 1.5 seconds on the benchmark repositories.

Details: `specs/001-repository-qa/research.md` R2.
