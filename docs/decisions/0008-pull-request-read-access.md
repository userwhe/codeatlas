# ADR 0008: Read-only pull request access for reviews

- **Status**: Accepted
- **Date**: 2026-10-06
- **Amends**: The permissions in ADR 0005
- **Scope**: `specs/003-pr-review` and later features that read pull requests

## Context

Users review open pull requests of connected repositories. CodeAtlas must list them and read each
one's head and base commits, title, and description. The App has only `Contents: read` and
`Metadata: read` (ADR 0005), and CodeAtlas makes no write to GitHub.

GitHub's documentation, checked on 2026-10-06:

- Listing pull requests on a private repository needs `Pull requests: read`.
- Reading one pull request accepts either `Pull requests: read` or `Contents: read`.
- Comparing commits and downloading archives need `Contents: read`.
- An installation keeps its old permissions until its owner approves an update.

## Options considered

1. Add `Pull requests: Read-only`. Request-time reads use the signed-in owner's user token, and
   review jobs use installation tokens with Contents only.
2. Keep the permissions, and have the user type a pull request number instead of choosing from a
   list.
3. Add the permission and subscribe to `pull_request` webhook events, storing each pull request's
   state.

## Decision

Option 1:

- **Permissions**: repository `Contents: read`, `Metadata: read`, and `Pull requests: read`.
  There is no write permission, and no new event subscription.
- **Request-time reads**: the pull request list, submission, and freshness checks use the owner's
  user token. GitHub honors it only where both the owner and the App can read the pull request.
- **Jobs**: compare the pinned commits and download their archives with installation tokens. No
  pull request endpoint is called.
- **Pending approval**: when the list is refused, the API reads the installation's `permissions`
  with the App JWT. If `pull_requests` is missing, it explains that the installation owner must
  approve the update, and links to the settings page. Submission and freshness keep working,
  because reading one pull request also accepts Contents.

## Trade-off

- **Gained**:
  - Users choose from the open pull requests (spec FR-001).
  - Still read-only, with the smallest permission that allows listing.
  - Jobs need nothing beyond Contents.
- **Accepted**:
  - Owners of existing installations must approve the change on GitHub before private
    repositories list their pull requests.
  - Freshness costs one GitHub call per page view, instead of being pushed by events.
- **Revisit when**:
  - reviews should start automatically on pull request activity, which needs `pull_request`
    events; or
  - a feature writes to pull requests, for example posting a reviewed draft. That needs a write
    permission, and each write needs the user's explicit confirmation of a shown draft.

Details: `specs/003-pr-review/research.md` R1, R2, and R10.
