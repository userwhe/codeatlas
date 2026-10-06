# ADR 0005: One GitHub App for sign-in and repository access

- **Status**: Accepted
- **Date**: 2026-10-03
- **Scope**: All features; permissions are revisited by any feature that needs writes or webhooks

## Context

Users sign in with GitHub. CodeAtlas must read only repositories that the user can access and
has explicitly allowed. It must not store long-lived broad credentials, and it must never execute
repository content.

## Options considered

1. One GitHub App, providing both user authorization for sign-in and installation tokens for
   repository reads.
2. An OAuth App for sign-in, plus a GitHub App for repository access.
3. Personal access tokens supplied by users.

## Decision

Option 1:

- **Permissions**: repository `Contents: read` and `Metadata: read` only. The first feature used no
  webhooks. Since `specs/002-push-reindexing`, the webhook is active and subscribed to `push`,
  with permissions unchanged (ADR 0007).
- **Access check** (revised 2026-10-05): `GET /repositories/{id}` with the user's token, then
  `GET /repos/{owner}/{repo}/installation` with the App JWT. The covering installation must also
  appear in `GET /user/installations`, which runs concurrently with the other two calls. A user
  token reaches every public repository, so without this listing another account's installation
  would be used. The check runs at connection and again at the start of each indexing job. The
  installation-scoped repository list is used only to fill the connect dialog.
- **Source fetch**: a commit tarball, streamed with size limits and extracted with path and link
  checks. No git binary and no hooks.
- **Credentials**: user tokens are encrypted at rest. Installation tokens are minted per job and
  never stored.

## Trade-off

- **Gained**:
  - One consent flow.
  - Least-privilege, revocable, per-repository access.
  - No secrets pasted by users.
- **Accepted**:
  - Developers must register a GitHub App even for local work. A fake gateway covers offline
    development and tests.
  - Write permissions added later require installation owners to approve the change. Any write
    must also be drafted first and run only after the user confirms it.
- **Revisit when**: a feature needs writes. The webhook case was resolved by ADR 0007.

Details: `specs/001-repository-qa/research.md` R5.
