# ADR 0011: Terraform defines the environment, and releases run only after approval

- **Status**: Accepted
- **Date**: 2026-10-07 (revised 2026-10-08: build before approval without cloud credentials, one
  release role, immutable OIDC subject, and a push-only gate)
- **Scope**: `specs/004-pilot-deployment`

## Context

The pilot must be rebuildable from a recorded definition, every environment change must be
previewed and approved, and every release must be approved, checked after it starts, and rolled
back on failure. Writes to external systems run only after the developer confirms what will happen.
No long-lived cloud keys may be stored in the repository or CI.

Two facts shape the release pipeline. A `workflow_run` trigger's branch filter matches the
triggering run's branch name, which a fork's pull request can also call `main`. And this repository
uses GitHub's immutable OIDC subject format, `repo:<owner>@<owner ID>/<repo>@<repo ID>`, so trust
policies written as `repo:<owner>/<repo>` would reject its tokens.

## Options considered

1. Terraform for infrastructure; GitHub Actions builds images without cloud credentials, and only
   after approval in a protected environment pushes them and releases through SSM Run Command,
   using one OIDC role.
2. As option 1, but a second OIDC role pushes images to ECR as soon as CI passes, before approval.
3. CloudFormation or the CDK, with CodeDeploy for releases.
4. Console changes and a runbook; releases over SSH from CI.
5. Releases on every merge without approval, through a pull-based updater on the host.

## Decision

Option 1:

- **Infrastructure**: Terraform in two root modules, `infra/shared/` (registry, bucket, alert topic,
  OIDC provider and release role, hosted zone, budget) and `infra/stack/` (one environment:
  `pilot`, `drill`, or `loadtest`). State is in S3 with Terraform's native lock file (Terraform
  1.11 or later). The developer runs `plan`, reviews it, and applies the saved plan, with
  short-lived IAM Identity Center credentials. Secret values never enter Terraform.
- **Releases**: `release.yml` runs only for a successful CI run of a push to this repository's
  `main`, or for a dispatched commit that is an ancestor of `main` with successful CI. It builds
  both images tagged with the commit and keeps them as workflow artifacts, writes a release summary
  (commit, commits since the running version, schema changes), and waits for the developer's
  approval in the `pilot` environment. Only then does it push the images to ECR and run a host
  script that migrates, starts the new images, checks readiness, the web app, and the reported
  version, and restores the previous images if the check fails. The trigger is filtered to `main`,
  and only the approval-gated job joins the concurrency group, so runs whose jobs are skipped
  never displace a pending release.
- **Credentials**: one OIDC role, trusted only for `<subject prefix>:environment:pilot`, where the
  prefix is a Terraform variable read from the repository's OIDC settings. It can push to the two
  image repositories, upload the release bundle, run the release command on the pilot instance,
  read the command's results, and publish alerts.

## Trade-off

- **Gained**:
  - Every change to the cloud account is shown before it happens, and approved; nothing is written
    to AWS before a release is approved.
  - A fork's pull request cannot start a release, and its code never runs with cloud credentials.
  - The environment can be rebuilt, or copied for a drill or a load test, from code.
  - No AWS key exists in GitHub, and the host has no inbound administration port.
- **Accepted**:
  - Each release waits for a person, even for small changes, and a run waiting for approval blocks
    newer ones until it is approved or rejected.
  - Images travel through workflow artifacts, which adds a minute or two after approval.
  - Schema changes must keep the previous version working (expand, then contract in a later
    release), because rollback restores images, not the database.
  - The developer applies infrastructure from their machine rather than from CI.
  - The trust policy depends on the repository's numeric owner and repository IDs; transferring or
    recreating the repository changes the prefix.
- **Revisit when**:
  - more than one person releases or changes infrastructure: plan and apply in CI with reviews;
  - releases become frequent enough that approval slows the work; or
  - the deployment moves to ECS (ADR 0010), which replaces Run Command with service updates.

Details: `specs/004-pilot-deployment/research.md` R5 to R8.
