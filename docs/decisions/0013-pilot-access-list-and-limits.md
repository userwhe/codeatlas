# ADR 0013: A pilot access list and pilot-wide daily limits

- **Status**: Accepted
- **Date**: 2026-10-07
- **Scope**: `specs/004-pilot-deployment`

## Context

On the public internet, anyone with a GitHub account can complete sign-in (ADR 0005). The pilot is
for at most 10 invited users. Every question and review costs model spend, and the workspace
allowances (20 questions and 10 reviews a day) bound each workspace, not the pilot. A visitor who
is not invited must leave no workspace or token behind. The developer must be able to add and remove
users without a release.

## Options considered

1. A `pilot_users` table keyed by GitHub user ID, checked at sign-in before anything is stored, and
   managed with operator commands on the host; plus a `pilot_usage_counters` table with pilot-wide
   daily limits.
2. A list of logins in an environment variable.
3. Membership of a GitHub organization or team.
4. Open sign-up, bounded only by the daily limits.

## Decision

Option 1:

- `complete_login` checks `pilot_users` right after GitHub identifies the user. A user not on the
  list gets an invitation page and a denied audit event; no user, workspace, token, or session is
  written. The check always applies in production.
- `python -m codeatlas.ops pilot-users add|remove|list|delete-data` manages the list. Adding
  resolves the login to GitHub's numeric ID, because logins can be renamed and reused. Removing
  revokes the user's sessions in the same transaction.
- Pilot-wide limits of 30 questions and 15 reviews a day, reserved in the same transaction as the
  workspace allowance, keep the worst-case model spend under $15 a day.
- Migration `0004_pilot_access` adds both tables and three audit actions.

## Trade-off

- **Gained**:
  - Uninvited visitors cost nothing and leave nothing but an audit row.
  - Access changes take effect at once, are audited, and are restored with backups.
  - Daily model spend has a known ceiling.
- **Accepted**:
  - Adding a user needs a command on the host, through Session Manager.
  - Heavy use by a few users can exhaust the pilot-wide limit for everyone that day.
  - Removing a user keeps their data until the developer deletes it.
- **Revisit when**:
  - the pilot opens to more users or to sign-up: replace the list with invitations or a waitlist;
  - users ask to delete their own data: add self-service account deletion; or
  - the pilot-wide limits are reached on more than a few days.

Details: `specs/004-pilot-deployment/research.md` R13 and R14.
