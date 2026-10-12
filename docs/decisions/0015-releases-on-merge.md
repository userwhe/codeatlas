# ADR 0015: Every commit merged into `main` is released once CI passes

- **Status**: Accepted
- **Date**: 2026-10-12
- **Scope**: `specs/004-pilot-deployment`; supersedes ADR 0011

## Context

Releases must start only from `main` after the automated checks pass, apply schema changes before
the new version starts, move traffic only to a version that passes its readiness check, keep the
previous version serving on failure, lose no jobs, and allow a rollback within 5 minutes. The
repository and CI must hold no credentials for the hosting platform. ADR 0011 added an approval
of each release through a protected GitHub environment and a release workflow with an OIDC role;
the developer chose instead to release every merge without approval (ADR 0014's platform, spec
FR-020).

## Options considered

1. Railway's GitHub integration with "Wait for CI", `api`'s pre-deploy command for migrations, and
   healthchecks.
2. An approval workflow in GitHub Actions that deploys with the Railway CLI (ADR 0011's flow).
3. Deploying from GitHub Actions with the Railway CLI after CI, without approval.
4. Releasing only when a tag is pushed.

## Decision

Option 1:

- `web`, `api`, `worker`, and `backup` deploy from `main`; from the first deployment, Railway
  waits until every GitHub Actions check suite on the commit passes and skips the deployment if
  one fails. Pull request environments are off.
- `api`'s pre-deploy command, `python -m codeatlas.ops migrate`, runs before the new version
  starts: under a PostgreSQL advisory lock, it upgrades the schema when the database's revision is
  one the release knows, and skips with a log line when the database is ahead, so a rolled-back API
  starts against a newer schema. A failure keeps the previous version serving.
- The services deploy in parallel, so `web`, `worker`, and `backup` each run a pre-deploy release
  gate that waits, for up to 20 minutes, until `api` reports their own commit at `/version`.
  `api` reports it only after migrating and passing its readiness check, so a failure of either
  stops the whole release, and every service keeps its previous version.
- `api` (`/readyz`) and `web` (`/`) receive traffic only after their healthchecks pass. Overlap and
  draining times keep in-flight requests answered, and give the old worker 30 seconds after
  `SIGTERM`; leases and fencing tokens cover a job cut short.
- A CI job lists every added, changed, deleted, or renamed migration on every pull request, with
  the expand-then-contract rule, so the developer checks schema changes in review before merging.
- Rollback is Railway's "Rollback" on a service's previous deployment: one action per service, no
  rebuild, `api` first. On Hobby the previous image is kept for 72 hours after it is replaced;
  after that, the earlier commit is redeployed and builds again.

## Trade-off

- **Gained**:
  - No release workflow, host scripts, image registry, or platform credentials in CI.
  - About 10 minutes from merge to serving: about 5 for CI, then the builds.
- **Accepted**:
  - The merge is the only confirmation of a release; a direct push to `main` also releases, so the
    operations guide says to merge through pull requests.
  - Between `api`'s switch and the gated services' switches, the API may run a newer commit than
    the web app and the worker; changes that need both follow the expand-then-contract rule.
  - Two small gate programs, one in each image, because the web image has no Python.
  - Rollback is per service and is done for the whole release, `api` first; after the 72-hour
    window it takes a build, about 15 minutes instead of 5.
  - "Wait for CI" waits for every check suite on the commit, including ones from other GitHub Apps.
- **Revisit when**:
  - a bad release reaches pilot users: add an approval step or a staging environment;
  - services need a strict deploy order; or
  - the pilot runs more than one replica of `api`.

Details: `specs/004-pilot-deployment/research.md` R6 and R7.
