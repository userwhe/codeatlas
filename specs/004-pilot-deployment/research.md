# Research: Pilot Deployment

Phase 0 output for [plan.md](plan.md). Each entry records the decision, the rationale, and the
alternatives considered. This feature builds on `specs/001-repository-qa`,
`specs/002-push-reindexing`, and `specs/003-pr-review`; references such as "001 R11" point to
their research notes. AWS prices are on-demand prices in `us-east-1`, checked against AWS's
pricing pages on 2026-10-07; R19 adds them up.

## Unknowns resolved

| Unknown | Resolution |
| --- | --- |
| Where and on what the pilot runs | R1, ADR 0010 |
| How the host is set up, patched, and restarted | R2 |
| TLS, routing, and what the internet can reach | R3 |
| Rate limits for endpoints without a session | R4 |
| Production settings and credentials | R5 |
| Release images and where they are stored | R6 |
| How a release is built, approved, applied, checked, and rolled back | R7, ADR 0011 |
| How the hosting environment is defined and changed | R8, ADR 0011 |
| Where logs go and how they are searched | R9 |
| Metrics, the dashboard, and alerts within the free tier | R10, ADR 0012 |
| Liveness, readiness, and the running version | R11 |
| Backups, restore, and recovery from host loss | R12 |
| The pilot access list | R13, ADR 0013 |
| Pilot-wide daily limits and the worst-case model spend | R14, ADR 0013 |
| How and where the load test runs | R15 |
| The benchmark evaluation and the project's own sets | R16 |
| README, diagram, recording, and operations guide | R17 |
| Tests and checks | R18 |
| Monthly cost | R19 |

New tools and services, each tied to a requirement: Terraform (FR-016, FR-025), Caddy (FR-001),
and these AWS services: EC2, EBS, ECR, S3, SSM, CloudWatch, SNS, Route 53, Budgets, and IAM
(R1 to R12). CI gains `bats` to test the host scripts (R18). The application gains no new Python
or npm dependency.

## Deviations from `docs/design/codeatlas-v1.md`

| Design document | This increment | Revisit when |
| --- | --- | --- |
| ECS on Fargate behind an HTTPS load balancer | One EC2 host running Docker Compose behind Caddy (R1, R3, ADR 0010) | Availability needs more than one host, releases must not interrupt service, or the API and worker need to scale separately |
| A private RDS PostgreSQL instance | PostgreSQL in a container on the host, with its data on a separate EBS volume and nightly logical backups to S3 (R12) | Up to 24 hours of data loss or a 2-hour recovery becomes unacceptable |
| SQS, a transactional outbox, and S3 artifact storage | Unchanged from 001: the PostgreSQL job queue (ADR 0003) and PostgreSQL as the only data store (ADR 0002). S3 holds only backups and release bundles | ADR 0002 and ADR 0003 revisit conditions |
| A staging environment with a smoke test | No standing staging. CI runs the browser tests in fake mode; the release checks the pilot itself and rolls back; temporary environments serve the load test and the recovery exercise (R7, R15, R12) | Releases start to fail their post-release check |
| Traces | Logs carry request, job, run, and snapshot identifiers; no tracing (R9) | A request path spans more than the API and the database |
| All listed metrics as metrics | Alarm inputs only as custom metrics; the rest from log queries (R10, ADR 0012) | A log query becomes too slow or costly for the dashboard |
| An operations view for jobs | None; the dashboard, log queries, and the `jobs` table | The developer needs it more than weekly |
| Distinct API and worker IAM roles | No container has AWS credentials at all; only host scripts use the instance role (R2, R5) | A container needs to call AWS |
| Edge rate limiting | An in-process limiter in the API, before any other processing (R4) | The API runs in more than one process |

## R1. Hosting: one EC2 host with Docker Compose (ADR 0010)

**Decision**:

- **Host**: one `t4g.medium` instance (2 Arm vCPUs, 4 GiB) in `us-east-1`, in the account's
  default VPC, with an Elastic IP. It runs the development services, `db`
  (`pgvector/pgvector:pg17`), `api`, `worker`, `web`, and the one-shot `migrate`, with `caddy` in
  front of them.
- **Disks**: a 20 GiB encrypted gp3 root volume for the OS, images, and swap, and a separate 20 GiB
  encrypted gp3 data volume that holds PostgreSQL's data directory, Caddy's certificates, the
  backup staging directory, and the release state. In the pilot, the data volume has
  `prevent_destroy`, so replacing the host keeps the data (R12); the temporary environments use an
  unprotected volume, so they can be destroyed (R8).
- **CPU credits**: standard mode. A `t4g` instance launches in unlimited mode by default, which
  bills CPU use above the baseline; standard mode throttles instead, keeping the cost fixed
  (R19).
- **Memory**: PostgreSQL gets `shared_buffers=512MB` and `max_connections=50`. A 2 GiB swap file
  absorbs the worker's peaks while it reads archives (up to 100 MiB of eligible files, 001 FR-009).
- **Sizing**: the load test (R15) records CPU and memory. If peak memory stays under half of the
  host, a `t4g.small` is tried; the ADR records the result.
- **Architecture**: Arm (Graviton) is about 20% cheaper than the matching x86 size. Every image is
  available for `arm64`: Python 3.13 slim, Node.js 24 slim, `pgvector/pgvector:pg17`, and Caddy 2.
  Images are built natively on GitHub's free Arm runners for public repositories (R6).

**Rationale**:

- Pilot traffic is a few users. Managed services charge fixed hourly fees whether or not they
  serve traffic: an ALB, an RDS instance, a public IPv4 address per Fargate task, or a NAT gateway.
  On one host those fees disappear, and the four containers share one pool of CPU and memory.
- The containers are already built and tested together in Compose (README, CI's `e2e` job). The
  pilot runs the same topology, so the gap between tested and deployed is small.
- Containers stay stateless except `db`, so moving to ECS and RDS later changes the infrastructure
  definition, not the application.

**Alternatives considered**:

- **ECS on Fargate, RDS, and an ALB** (the design document): about $80 a month at pilot size, most
  of it fixed fees. Rejected for cost; the ADR records the signals that would justify it.
- **Lightsail** (4 GiB plan, about $24 with storage and an IPv4 address included): cheaper, but
  Lightsail instances cannot use IAM instance roles. Backups, logs, metrics, and image pulls would
  need long-lived access keys on the host, which FR-008 and FR-024 rule out.
- **A platform service** (Fly.io, Render, Railway): least operations work, but less control over
  backups, networking, and alarms, and a second vendor next to the model providers.
- **Kubernetes** (EKS, or k3s on the host): a control plane to run or pay for, with no current
  need (design document, "Kubernetes: Defer").

## R2. Host image, bootstrap, patching, and restarts

**Decision**:

- **OS**: Ubuntu Server 24.04 LTS for `arm64`, from Canonical's public SSM parameter. The AMI
  includes the SSM Agent, so the host needs no SSH key and port 22 stays closed. Administration
  uses Session Manager and Run Command.
- **Bootstrap**: cloud-init user data, rendered by Terraform, installs Docker Engine and the
  Compose plugin from Docker's apt repository, the AWS CLI v2, and the CloudWatch agent. It then:
  - finds the data volume by its EBS volume ID under `/dev/disk/by-id/`, creates an ext4 file
    system only if the volume has none, and mounts it by UUID at `/var/lib/codeatlas`;
  - adds a `docker.service` drop-in with a `[Unit]` section holding
    `RequiresMountsFor=/var/lib/codeatlas`, then runs `systemctl daemon-reload`, so Docker never
    starts PostgreSQL on an empty root-volume directory when the mount fails;
  - creates the swap file, `/opt/codeatlas`, and the `codeatlas-compose` wrapper (R7); and
  - in the pilot only, installs the backup and certificate-check timers (R10, R12).
- **First boot only**: cloud-init runs user data once per instance. Terraform sets
  `user_data_replace_on_change = true`, so a change to the bootstrap shows in the plan as a host
  replacement, which the developer approves and follows with a release (R12), instead of a silent
  stop and start that would leave the change unapplied.
- **Patching**: `unattended-upgrades` (on by default in Ubuntu) applies security updates daily.
  Automatic reboots are enabled at 04:30 UTC when an update requires one. Each reboot exercises
  FR-017, and its minute or two of downtime counts against SC-001.
- **Restarts**: every service has `restart: unless-stopped`, and Docker starts at boot, so all
  services return after a reboot without manual steps (FR-017). Jobs interrupted by a restart
  resume through the existing leases: a lease lapses 60 seconds after its last heartbeat, and
  another attempt claims the job (001 FR-030).
- **Metadata service**: IMDSv2 is required, with a hop limit of 1. Processes in containers sit one
  network hop further away, so they cannot obtain the instance role's credentials. None of them
  needs AWS: the Docker daemon ships their logs (R9), and their metrics travel inside the logs
  (R10). Only host scripts (release, backup, certificate check) use the role.
- **Host recovery**: EC2's simplified automatic recovery is on by default for this instance type.
  It restarts the instance on new hardware after a system status check failure, keeping the
  volumes and the Elastic IP.

**Rationale**: Ubuntu's apt repository from Docker ships the Compose plugin, and unattended
security updates are on by default. Closing SSH removes the most common attack path and the need to
distribute a key.

**Alternatives considered**:

- **Amazon Linux 2023**: first-party and also includes the SSM Agent, but its repositories have no
  Docker Compose plugin, so the plugin would be installed and updated by hand.
- **SSH with a key pair**: a long-lived credential and an open port, both avoidable.
- **A prebuilt AMI (Packer)**: faster boots, but another build pipeline, with no current need.

## R3. Edge: TLS, routing, and the public surface

**Decision**:

- **Caddy 2.10** (`caddy:2.10`, pinned; the log filters below need 2.8 or later) runs as the
  `caddy` container and is the only service that publishes ports (80 and 443). It obtains and
  renews a Let's Encrypt certificate for the environment's domain name automatically, and
  redirects HTTP to HTTPS (FR-001). It adds `Strict-Transport-Security: max-age=31536000`.
- **Routes**: `/v1/*`, `/auth/*`, `/webhooks/*`, `/healthz`, `/readyz`, and `/version` go to `api`;
  everything else goes to `web`. Caddy sets `X-Forwarded-For` and `X-Forwarded-Proto`; uvicorn runs
  with `--proxy-headers`. In development, Next.js keeps proxying `/v1` and `/auth` as before.
- **Logs**: JSON to stdout. The OAuth callback carries `code` and `state` in its query (as 001
  R16 handles for uvicorn's log), so Caddy's log filter deletes those parameters from
  `request>uri` and from the `resp_headers>Location` of redirects, and deletes
  `request>headers>Referer`. A global `log default` block applies the same filter to Caddy's error
  logs, which otherwise record the raw URI (for example on a 502 while the API restarts).
- **Security group**: inbound TCP 80 and 443 from anywhere over IPv4 (the default VPC has no IPv6
  range); nothing else. PostgreSQL
  publishes no port; only containers on the Compose network reach it (FR-007).
- **Domain**: the developer's domain, in a Route 53 hosted zone managed by Terraform. Each
  environment has its own name, for example `codeatlas.example.dev`, `drill.example.dev`, and
  `loadtest.example.dev`. The GitHub App's webhook and callback URLs use the name, so a rebuilt
  host keeps working (spec edge case).

**Rationale**: Caddy gives automatic certificates and redirects with a ten-line configuration and
no extra service. An ALB with an ACM certificate would add about $18 a month for one target.

**Alternatives considered**:

- **nginx with certbot**: two components and a renewal timer instead of one.
- **An ALB with ACM, or CloudFront**: managed TLS and room for AWS WAF, but fixed monthly fees
  for one host.

## R4. Rate limits for endpoints without a session (FR-006)

**Decision**:

- An HTTP middleware in the API, registered last so that it runs before the Origin check and
  every route, limits
  requests to `/auth/*` and `/webhooks/*` to 60 per minute per client address. It keeps a sliding
  window of timestamps per address in memory and forgets idle addresses.
- Excess requests get `429 rate_limited` with `Retry-After`, before any signature check, database
  access, or audit row. This closes the gap ADR 0007 accepted ("until edge rate limiting arrives
  with deployment").
- The client address is the one uvicorn derives from `X-Forwarded-For` set by Caddy. The API's port
  is published to no network except the Compose network, so only Caddy (and `web`, which does not
  proxy in production) can reach it.
- Settings: `RATE_LIMIT_PER_MINUTE` (default 60; 0 disables, for tests and the load test).

**Rationale**: The API runs one uvicorn process (001's latency targets were measured that way), so
an in-memory window is exact. It needs no new dependency or service, and unit tests cover it.

**Alternatives considered**:

- **Caddy's rate-limit module**: a third-party module that needs a custom Caddy build.
- **AWS WAF**: needs an ALB or CloudFront in front (R3).
- **A shared counter in PostgreSQL or Redis**: needed only when the API runs in several processes.
  That is the revisit condition; the limiter's interface stays the same.

## R5. Production settings and credentials (FR-008 to FR-010)

**Decision**:

- **Store**: SSM Parameter Store, standard tier, under `/codeatlas/<environment>/`. Secrets are
  `SecureString` parameters encrypted with the AWS managed key. Standard parameters are free.
- **Who writes them**: the developer, with `deploy/put-secrets.sh`, which reads values from local
  files or a prompt and calls `aws ssm put-parameter`. `--set NAME=VALUE` and `--unset NAME`
  change one optional setting, for example a temporary limit. Terraform never sees secret values,
  so they are not in Terraform state.
- **Who reads them**: `deploy/render-config.sh` runs on the host during each release. It writes
  `/opt/codeatlas/runtime/app.env` (mode 0600) and the GitHub App's private key file (mode 0400,
  owned by the containers' `app` user). Only `api`, `worker`, and `migrate` mount them; `web` and
  `caddy` receive no secret. `migrate` needs the key file too, because `alembic/env.py` loads the
  full settings, and production validation requires a readable key.
- **Parameters**: `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_CLIENT_ID`,
  `GITHUB_APP_CLIENT_SECRET`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET`,
  `TOKEN_ENCRYPTION_KEY`, `GEMINI_API_KEY`, `VOYAGE_API_KEY`, and `POSTGRES_PASSWORD`, plus
  non-secret settings such as the mode and the limits. Which are required depends on the mode:
  production needs all of them; fake mode (the load test) needs only `TOKEN_ENCRYPTION_KEY` and
  `POSTGRES_PASSWORD`. The full list is in [contracts/operations.md](contracts/operations.md).
- **Derived settings**: render-config writes `APP_ORIGIN=https://<hostname>` from the host's own
  name, never from SSM, so the drill, which reads the pilot's parameters, and the load test get
  their own origin for OAuth callbacks and the Origin check. It also writes `DATABASE_URL` and
  `METRICS_ENVIRONMENT`, and forces `EMIT_METRICS=0` outside the pilot, so the drill adds no
  metrics even though it inherits the pilot's setting.
- **Quoting**: Compose interpolates `$` in unquoted `env_file` values and cuts a value at ` #`, so
  render-config writes every value in single quotes (a `'` inside a value becomes `'\''`), which
  Compose takes literally. Generated secrets use only hexadecimal or URL-safe Base64 characters.
- **Startup validation** (FR-009): in production, `Settings` collects every missing required
  setting and raises one error that lists their names: the GitHub App settings, a readable private
  key file, the webhook secret, the token encryption key, both model provider keys, an `https`
  `APP_ORIGIN`, and a `DATABASE_URL` other than the development default (which embeds a password).
  Fake externals remain refused outside development and test.
- **No values in errors**: today a settings error prints every input value, secrets included,
  because pydantic includes `input_value` in validation errors. This was reproduced on 2026-10-07
  with a production configuration missing the webhook secret. `Settings` sets
  `hide_input_in_errors=True`, and a unit test asserts that no value appears in the message.
  `ValidationError.errors()` and `.json()` still include the input, so the code never logs them.
- **Pilot GitHub App** (FR-010): a second App registration, `CodeAtlas`, owned by the developer,
  with the same read-only permissions and events as the development App, the pilot's callback and
  webhook URLs, and its own webhook secret. It is public, so pilot users can install it on their
  repositories.
- **Replacing a credential**: update the parameter, then run the release script with the running
  commit (`release.sh --refresh-config`), which recreates `api` and `worker`. Replacing
  `TOKEN_ENCRYPTION_KEY` makes stored GitHub tokens unreadable; users are asked to sign in again,
  as 001 already handles. `POSTGRES_PASSWORD` takes effect in the database only when the data
  directory is created, so replacing it means `ALTER ROLE codeatlas PASSWORD ...` in the `db`
  container first, then the parameter, then the refresh; `put-secrets.sh --overwrite` refuses it
  and points to that procedure. Each credential's steps are in the operations guide.

**Rationale**: Parameter Store is free at this size and readable with the instance role. Keeping
secret values out of Terraform keeps the state file free of credentials.

**Alternatives considered**:

- **Secrets Manager**: built-in rotation, which no credential here can use (GitHub and the model
  providers rotate by hand), at $0.40 per secret per month.
- **Terraform-managed secrets**: values would sit in the state file.
- **An `.env` file copied to the host by hand**: no audit trail and no reproducible rebuild.

## R6. Release images and registry

**Decision**:

- Two private ECR repositories, `codeatlas/api` and `codeatlas/web`, with immutable tags, basic
  scanning on push, and a lifecycle rule that keeps the 20 newest images. Each image is tagged with
  its full commit SHA (FR-019). Images are built before approval but pushed only after it (R7);
  the release job skips pushing a tag that already exists.
- **API image**: the existing `backend/Dockerfile`, plus a `CODEATLAS_RELEASE` build argument
  exposed as an environment variable, and the fixture repositories copied to
  `/app/tests/fixtures` (212 KB), so that the load test's fake mode works in the release image.
  Fake mode stays refused in production (FR-009).
- **Web image**: `frontend/Dockerfile` becomes multi-stage. The `dev` target keeps today's
  `npm run dev` for Compose; the `production` target builds Next.js with `output: "standalone"`,
  copies `.next/standalone` and `.next/static` (the project has no `public` directory), runs
  `node server.js` as a non-root user with `HOSTNAME=0.0.0.0`, and bakes `NEXT_PUBLIC_RELEASE`
  into the footer.
- Builds run natively on GitHub's `ubuntu-24.04-arm` runners, free for public repositories since
  August 2025.

**Rationale**: ECR is pulled by the host with its instance role, so no registry credential exists
anywhere; immutable tags make a tag mean one build. Nothing reaches the registry before the
developer approves the release.

**Alternatives considered**:

- **GitHub Container Registry**: free and simple for public images. Rejected because the host would
  pull from outside AWS, and immutable tags are not enforced.
- **Building on the host**: no registry, but builds compete with the pilot for CPU and memory, and a
  rollback would need a rebuild.
- **QEMU cross-builds on x86 runners**: several times slower than native Arm runners.

## R7. Release pipeline and approval (FR-019 to FR-024, ADR 0011)

**Decision**: a `release.yml` workflow in GitHub Actions.

- **Trigger**: `workflow_run` when the CI workflow completes, filtered to `branches: [main]`, and
  `workflow_dispatch` with a commit. Every job runs only when the run was dispatched, or when the
  CI run concluded `success`, its event was `push`, its head repository is this repository, and
  its head branch is `main`. `workflow_run`'s branch filter matches the triggering run's branch
  name, and a fork's pull request can also name its branch `main`, so the filter alone is not
  enough; it only keeps pull requests from other branches from creating runs. Jobs check out the
  event's `head_sha`, never the default branch's tip. On dispatch, the `summary` job verifies that
  the commit is an ancestor of `origin/main` and that CI succeeded for it
  (`gh run list --workflow CI --commit <sha>`, with the job's `actions: read` permission), and
  fails otherwise (FR-019).
- **Job `build`** (no cloud credentials): a matrix of `api` and `web` on `ubuntu-24.04-arm` builds
  each image for the commit and saves it as a compressed workflow artifact kept for 3 days.
  Nothing is written to AWS before approval (FR-020).
- **Job `summary`**: reads `https://<domain>/version` for the running commit and writes the release
  summary to the workflow run: the commit, `git log` since the running commit, and every file added
  under `backend/alembic/versions/` (FR-020). A schema change is shown first, with a reminder of the
  expand-then-contract rule.
- **Job `release`**: runs on `ubuntu-24.04` (pinned: loading and pushing Arm image archives is
  proven on its Docker, while `ubuntu-latest` moves to a new Docker image store in late 2026), and
  uses the GitHub environment `pilot`, which has the developer as a required reviewer and allows
  only `main`. The job waits until the developer approves the run; the approval
  covers exactly that run's commit (FR-020). Then it assumes `codeatlas-release`, the only release
  role, which may push to the two ECR repositories, upload to `s3://<bucket>/releases/*`, send
  `AWS-RunShellScript` to instances tagged `codeatlas:environment=pilot`, read the command's
  results, and publish to the alert topic. The job:
  1. downloads both image artifacts, loads them, and pushes `codeatlas/api:<sha>` and
     `codeatlas/web:<sha>`, skipping a tag that already exists;
  2. uploads the `deploy/` directory for the commit as `releases/<sha>/deploy.tar.gz`;
  3. sends a Run Command that downloads and extracts the bundle into
     `/opt/codeatlas/releases/<sha>/` and runs its `release.sh <sha>`, with output to the log group
     `/codeatlas/pilot/releases`;
  4. probes `https://<domain>/readyz` and `https://<domain>/` once a second meanwhile, and
     reports the longest run of failures of either as the release's interruption (SC-009); and
  5. on failure, publishes an alert with the commit and exit status to the SNS topic (FR-022).
- **Host layout**:
  - `/opt/codeatlas/environment`: written by cloud-init (environment name, region, registry,
    bucket, hostname, parameter path).
  - `/opt/codeatlas/releases/<sha>/`: one extracted bundle per release.
  - `/opt/codeatlas/current`: a symbolic link to the running release's bundle.
  - `/opt/codeatlas/.env`: Compose's interpolation variables (`IMAGE_TAG`, `REGISTRY`,
    `ENVIRONMENT`, `AWS_REGION`, `CODEATLAS_HOSTNAME`), written by `release.sh`.
  - `/opt/codeatlas/runtime/`: the rendered configuration (R5).
  - `/usr/local/bin/codeatlas-compose`, installed by cloud-init: `docker compose
    --project-directory /opt/codeatlas -f /opt/codeatlas/current/compose.yml --env-file
    /opt/codeatlas/.env "$@"`. `compose.yml` sets `name: codeatlas`, and its relative paths
    (`./current/Caddyfile`, `./runtime/`) resolve under `/opt/codeatlas`. Operator commands,
    backups, restores, and the systemd units all use this wrapper and `/opt/codeatlas/current/`.
  - `/var/lib/codeatlas/state/running` and `previous` on the data volume: the commits of the
    running and previous releases, so a replaced host knows what to release (R12).
- **`release.sh <sha>`** on the host, from its bundle directory:
  1. renders configuration (R5);
  2. pulls only the `api` and `web` images, with `IMAGE_TAG=<sha>` passed in the environment (which
     overrides the env file), so third-party images such as `db` and `caddy` are not refreshed and
     restarted by a release;
  3. runs `alembic upgrade head` in the `migrate` container, the same way. Any failure up to here,
     including rendering, the registry login, or a pull, exits 2 with the previous version still
     serving and `/opt/codeatlas/.env` unchanged (FR-021);
  4. writes `.env` with the new tag, records the running commit as `previous`, points `current` at
     the new bundle, and runs `up -d`. Only `api`, `worker`, and `web` are recreated, and `caddy`
     too when the bundle's Caddyfile differs from the running one (`up -d --force-recreate
     caddy`), because a bind-mounted file stays pinned in a running container;
  5. checks, for up to 120 seconds, that `/readyz` returns 200, `/` returns 200, and `/version`
     returns the commit (FR-022); and
  6. on failure, points `current` and `.env` back at the previous release, starts it (recreating
     `caddy` if the Caddyfile differs), waits for readiness, and exits 3. It exits 4 if that also
     fails, or if the host has no previous release (the first release on a host), in which case it
     stops the new services.
- **`--refresh-config`** re-renders configuration and runs `up -d --force-recreate api worker` on
  the running tag; a plain restart would keep the containers' old environment.
- **Releasing a commit again by hand** (the drill, the load test, a replaced host): the images
  already exist and ECR tags are immutable, so the procedure skips pushing a tag that exists, and
  it uploads the bundle again from a checkout of the commit when `releases/<sha>/` has expired
  (after 30 days).
- **Jobs during a release** (FR-023): the worker receives `SIGTERM` and stops after its current
  job (001's worker). Compose waits 30 seconds (`stop_grace_period`). A job still running then is
  stopped; its lease lapses within 60 seconds and the new worker claims it as a new attempt. No job
  is lost, and fencing tokens prevent a duplicate publication (001 R3).
- **Interruption** (FR-023): recreating `api` and `web` takes seconds. Caddy keeps running and
  returns 502 while a container restarts.
- **Schema rule**: a release's migration must keep the previous version working (add, never drop
  or rename in the same release). The operations guide states it, and the summary flags every
  migration.
- **Rollback rehearsal** (SC-010): `workflow_dispatch` takes `expect_version`, which overrides the
  version the check expects, so a release can be made to fail its check without changing code.
- **Concurrency**: one release at a time, with `concurrency: release-pilot` on the `release` job
  only. At the workflow level, every run, including runs whose jobs are all skipped, would join the
  group and could cancel a pending release. A job waiting for approval holds the group, and GitHub
  replaces only jobs that have not started, so a stale job blocks newer ones until the developer
  rejects it. The operations guide says to reject stale runs.
- **Credentials** (FR-024): the release role trusts only this repository's OIDC tokens for the
  `pilot` environment. The repository uses GitHub's immutable subject format,
  `repo:<owner>@<owner ID>/<repo>@<repo ID>`, checked on 2026-10-08 with
  `gh api repos/<owner>/<repo>/actions/oidc/customization/sub`, so the trust policy takes the
  subject prefix as a Terraform variable. No AWS key is stored in GitHub.
- **Release records**: the GitHub deployment history (commit, approver, time, outcome) and the
  Run Command output in CloudWatch Logs. Nothing is stored in the database.

**Rationale**: Approval through a protected environment shows the developer the exact commit and
summary, and nothing outside GitHub is written until they approve, which is the project's rule
for writes to external systems. Building before approval keeps approval-to-serving short. Run
Command needs no inbound port and no SSH key.

**Alternatives considered**:

- **Pushing images before approval, with a build role trusted for `main`**: no artifact round
  trip, but it writes to the registry before the developer has seen the summary.
- **Release without approval on every merge**: simpler, but it writes to the cloud account without
  a confirmation of what is being released.
- **SSH from Actions**: an open port and a long-lived key.
- **CodeDeploy**: an agent, an application specification, and a deployment group for one host.
- **A pull-based updater on the host (for example Watchtower)**: no approval step, no migration
  order, and no post-release check.

## R8. Infrastructure definition (FR-016, FR-025, ADR 0011)

**Decision**:

- **Terraform** (1.11 or later, AWS provider 6), in two root modules:
  - `infra/shared/`: ECR repositories, the S3 bucket, the SNS alert topic and its email
    subscription, the GitHub OIDC provider and the release role (trusted for the subject prefix in
    `github_oidc_subject_prefix`, R7), the Route 53 hosted zone, and the monthly budget.
  - `infra/stack/`: one environment, selected by a variable file: the instance, its role and
    profile, the data volume, the Elastic IP, the security group, the DNS record, log groups, and,
    in the pilot only, alarms, the dashboard, and the health checks. `pilot`, `drill`, and
    `loadtest` each have their own state key.
  - The pilot's data volume is a separate resource with `prevent_destroy`; the temporary
    environments use another resource without it (`count` on the environment), because a
    lifecycle setting must be a literal.
  - The instance sets `user_data_replace_on_change = true` (R2), and the volume attachment sets
    `stop_instance_before_detaching = true`, so replacing the host does not try to detach a mounted
    volume from a running instance.
  - Permissions that are easy to get wrong: `ssm:GetParametersByPath` is authorized against the
    path's own ARN, so the role allows both `parameter<path>` and `parameter<path>/*`; Run Command
    output to CloudWatch needs `logs:DescribeLogGroups` on `*` and `logs:DescribeLogStreams`,
    `logs:CreateLogStream`, and `logs:PutLogEvents` on the releases log group; and the release
    role's `ssm:SendCommand` has one statement for the instances (with the
    `ssm:resourceTag/codeatlas:environment` condition) and one for the `AWS-RunShellScript`
    document.
- **State**: an S3 bucket with versioning and Terraform's native S3 lock file
  (`use_lockfile = true`, generally available since Terraform 1.11), so no DynamoDB table. The
  operations guide creates the bucket once with the AWS CLI before the first `terraform init`.
- **Preview and approval** (FR-025): the developer runs `terraform plan -out=<file>`, reads it, and
  then applies that saved plan. Applying anything else is not part of the guide. A plan that
  replaces the instance says so; the data volume survives (R12).
- **Developer credentials**: short-lived credentials from IAM Identity Center (`aws sso login`).
- **Variables kept out of the repository**: the alert email address, the domain, and the OIDC
  subject prefix go in `*.tfvars` files that `.gitignore` excludes; `*.tfvars.example` files are
  committed.
- **CI**: `terraform fmt -check` and `terraform validate` for both modules, `shellcheck` and the
  `bats` tests for the scripts in `deploy/` (R18), `docker compose config` for
  `deploy/compose.yml` with placeholder environment files, and `caddy validate` for the
  Caddyfile.

**Rationale**: A plan-then-apply workflow is the preview that FR-025 asks for. Terraform is the
most widely used tool for this, and its state records exactly what exists.

**Alternatives considered**:

- **CloudFormation or the CDK**: AWS-only, with change sets as the preview; the CDK adds a second
  language toolchain.
- **Pulumi**: infrastructure in Python, but a smaller ecosystem and a hosted state service by
  default.
- **Console changes and a runbook**: not reproducible, which FR-016 requires.
- **Terraform from CI with approval**: a long-lived or broad deploy role for infrastructure
  changes, which occur rarely; the developer's own short-lived credentials are enough.

## R9. Logs (FR-012)

**Decision**:

- Each service uses Docker's `awslogs` log driver in `non-blocking` mode with a 4 MiB buffer, so
  an unreachable CloudWatch never blocks the application. Log groups are
  `/codeatlas/<environment>/<service>` for `caddy`, `web`, `api`, `worker`, and `db`, plus
  `releases` and `host` (backup and certificate scripts), each kept for 7 days.
- The Docker daemon sends the logs with the instance role; containers hold no credentials (R2).
- The existing JSON logs (001 R16) gain two structured lines:
  - `request`: method, route template, status, and `duration_ms`, logged by an API middleware,
    with the request ID. In FastAPI 0.142 with Starlette 1.7 (checked on 2026-10-08), routers added
    with `include_router` appear in `app.router.routes` as `_IncludedRouter` objects without a
    `path`, so the middleware cannot search the route list. After `call_next` (and in `finally`
    when it raises), it reads `request.scope.get("route")`, which the router sets on the same
    scope to the route without its include prefix, and adds `/v1` back for paths under `/v1/`;
    without a route it logs `unmatched`. An unhandled exception bypasses the response, so the
    middleware logs status 500 for it before re-raising; and
  - `job_finished`: job kind, outcome, attempt, and `duration_ms`, with the job ID. The worker logs
    it for successes and scheduled retries; `queue._mark_failed` logs it for failures, inside a
    log context with the job ID, because failures found at claim time never reach the worker's
    context.
  The adapters log only retry warnings today; blocked, rejected, and exhausted calls raise without
  a log line. Each adapter therefore logs a warning with a `provider` field before each such raise,
  so the dashboard counts every provider error.
- `JsonFormatter` gains support for these structured fields, under an allow-list, so that a
  call site cannot add free text such as source or prompts.
- **Search**: CloudWatch Logs Insights queries across the log groups by `request_id`, `job_id`,
  `run_id`, or `snapshot_id`. The operations guide lists saved queries.

**Rationale**: The `awslogs` driver ships logs without an agent or credentials in the containers,
and Logs Insights reads the existing JSON fields without parsing rules.

**Alternatives considered**:

- **The CloudWatch agent tailing Docker's JSON files**: works, but adds file paths and rotation
  to manage for no gain.
- **Loki and Grafana on the host**: more memory on a 4 GiB host, and logs would be lost with it.
- **A third-party log service**: another account and another place that receives logs.

## R10. Metrics, dashboard, and alerts (FR-013, FR-014, ADR 0012)

**Decision**: a few custom metrics, for the alarms and for the queue and memory views, and log
queries for everything else.
CloudWatch's free tier covers 10 custom metrics, 10 alarms, 3 dashboards, and 5 GB of logs.

- **Custom metrics**, 9 in total (namespace `CodeAtlas` with dimension `Environment`, and the
  agent's `CWAgent`):
  - from the worker, through CloudWatch's embedded metric format (EMF) on stdout. CloudWatch
    extracts EMF from log events sent with PutLogEvents; the extra request header is optional, so
    the `awslogs` driver works.
    - A reporter thread, started by the worker next to its job loop, emits every 60 seconds
      `WorkerHeartbeat` (1), `QueuedJobs` (jobs `queued` or `retry_wait`), and
      `OldestRunnableJobAgeSeconds` (now minus `run_after` of the oldest job that `claim_next` could
      claim now: `queued` or `retry_wait`, `run_after` passed, and no running job of the same
      workspace and kind; 0 when none). The job loop runs periodic tasks only between jobs, and an
      indexing job can run for 15 minutes, so the reporter cannot be one of them.
    - `JobsFailed` (1) is emitted where a job is marked failed (`queue._mark_failed`, reached from
      both `fail` and `claim_next`), when the failure counts: retries used up, a timeout, or
      `internal_error`. Permanent failures caused by the input or by access (size limits, access
      lost, unsupported input) do not count.
    - The dimension's value comes from the `METRICS_ENVIRONMENT` setting, which Compose sets from
      the host's environment name.
  - from host scripts with `aws cloudwatch put-metric-data`: `BackupCompleted` and
    `CertificateDaysLeft`; and
  - from the CloudWatch agent: `mem_used_percent`, and `disk_used_percent` for the root and data
    volumes. With `drop_device` and `append_dimensions` of `InstanceId`, the disk metric's
    dimensions are exactly `InstanceId`, `path`, and `fstype` (`ext4`), which the alarms match.
  - EMF is written only when `EMIT_METRICS=1`, which only the pilot sets, so the temporary
    environments add no application metrics. Their CloudWatch agent metrics cost cents for the
    hours they exist.
- **Free built-in metrics**: EC2 `CPUUtilization` and `StatusCheckFailed`, and Route 53
  `HealthCheckStatus` and `HealthCheckPercentageHealthy`.
- **External checks**: two Route 53 HTTPS health checks every 30 seconds from several regions:
  `https://<domain>/readyz` (the API and the database) and `https://<domain>/` (the web app).
- **Dashboard** `codeatlas-pilot`: metric widgets for the above, plus Logs Insights widgets for the
  last 7 days: request count, 5xx count, and p95 latency by route group (searches, views,
  submissions, other); job duration, retries, and failures by kind; and provider errors by
  provider (FR-013).
- **Alarms** (10, pilot only), each with an alarm action and an OK action on the SNS topic, so a
  recovery notice follows every alert (FR-014):

  | Alarm | Condition |
  | --- | --- |
  | `site-down` | `/readyz` check `HealthCheckStatus` below 1 for 5 consecutive minutes |
  | `web-down` | `/` check `HealthCheckStatus` below 1 for 5 consecutive minutes |
  | `worker-down` | `WorkerHeartbeat` missing for 5 minutes (missing data is breaching) |
  | `jobs-waiting` | `OldestRunnableJobAgeSeconds` at least 1,800 (one worker runs one job at a time, and an indexing job may take 15 minutes) |
  | `jobs-failing` | `JobsFailed` sum at least 3 in one hour |
  | `root-disk-full`, `data-disk-full` | `disk_used_percent` above 80 |
  | `backup-missing` | `BackupCompleted` sum below 1 in each of 26 one-hour periods (26 of 26; missing data is breaching) |
  | `certificate-expiring` | `CertificateDaysLeft` minimum below 14 in each of 26 one-hour periods (26 of 26; missing data is breaching), so a low value or a missing day both fire it |
  | `host-status-check` | `StatusCheckFailed` at least 1 for 2 periods |

- **Daily alarms**: an alarm whose periods add up to more than a day is evaluated only once an
  hour, and CloudWatch ignores the missing-data setting whenever the evaluated range holds at
  least as many real data points as evaluation periods, so a single 26-hour period would keep the
  alarm quiet on the previous day's point. Twenty-six one-hour periods, all of which must breach,
  hold at most two daily points, so missing data always counts, and the alarm fires within about
  an hour after 26 hours without a backup.
- **Stopped worker**: the reporter stops with the worker, so `worker-down` fires within about 5
  minutes; `jobs-waiting` covers a worker that runs but falls behind.
- **Spending** (FR-014): an AWS Budgets monthly cost budget of $40 emails the developer when actual
  spend exceeds 80% and 100%, and when the forecast exceeds 80%. Forecasts need about five weeks of
  usage data, so in the first month only the actual-spend alerts work. Budgets sends each
  notification once per threshold and no recovery notice. The first two budgets are free.
- **Release failures**: the release workflow publishes to the same SNS topic (R7).
- **Subscription**: the developer confirms the SNS email subscription once.
- **Validation**: the fault tests in the quickstart trigger `site-down`, `web-down`, `worker-down`,
  `data-disk-full`, and `backup-missing`. `jobs-waiting`, `jobs-failing`, and
  `certificate-expiring` are checked by integration tests of their metrics and by
  `aws cloudwatch set-alarm-state`, which proves the notification path.
- **Permissions**: every environment's role may publish to `CWAgent` (the load test reads the
  agent's memory metric); only the pilot's may publish to `CodeAtlas`.

**Rationale**: Alarms need metrics, but most dashboard numbers do not. Querying the structured logs
for them costs nothing at pilot volume, while one custom metric per route, kind, and provider would
cost about $0.30 each per month. The whole monitoring setup fits in the free tier except the health
checks' HTTPS option.

**Alternatives considered**:

- **Prometheus and Grafana on the host**: richer dashboards, but more memory, and monitoring would
  fail with the host it watches.
- **CloudWatch Synthetics**: about $10 a month for a check every 5 minutes.
- **An external uptime service**: free tiers exist, but it is another account and another alert
  channel.
- **A custom metric per route and job kind**: about 20 more metrics, $6 a month, for numbers the
  dashboard can compute from logs.

## R11. Liveness, readiness, and the running version (FR-011, FR-019)

**Decision**:

- `GET /healthz` (exists): liveness. The process answers; nothing else is checked.
- `GET /readyz` (new): runs `SELECT 1` with a 2-second statement timeout. 200 `{"status": "ready"}`
  or 503 `not_ready`. It checks neither GitHub nor the model providers, so their outages leave the
  service ready (FR-011).
- `GET /version` (new): `{"commit": "<CODEATLAS_RELEASE>"}`, or `"development"` when unset.
- Compose health checks use `/healthz` for `api`; Caddy, the release check, and the external check
  use `/readyz`. The web footer shows the short commit.

**Rationale**: Separating liveness from readiness keeps a database outage from being mistaken for
a dead process, and the version endpoint lets the release check and the summary compare commits.

**Alternatives considered**: a single health endpoint (cannot tell the two failures apart); a
readiness check that calls the model providers (an outage would mark browsing and search as down,
against FR-011).

## R12. Backups, restore, and recovery (FR-015 to FR-018, SC-008)

**Decision**:

- **Nightly backup** at 03:30 UTC by a systemd timer, `/opt/codeatlas/current/backup.sh`:
  1. In the `db` container, one `psql -v ON_ERROR_STOP=1` session, with
     `/var/lib/codeatlas/backup` mounted at `/backup`: `BEGIN ISOLATION LEVEL REPEATABLE READ`;
     `SELECT pg_export_snapshot()` saved with `\gset` and passed to the shell with `\setenv`;
     `\! pg_dump -Fc --snapshot=...` into `/backup/codeatlas.dump`. psql treats any `\!` command
     that runs as a success, whatever its exit status, so the next lines check `:SHELL_ERROR`
     (PostgreSQL 16 and later) and raise an error when it is true, which stops the session. Then,
     in the same transaction, the row count of every table in the `public` schema and the Alembic
     revision, written as one JSON value with `\g (format=unaligned tuples_only=on)
     /backup/counts.json`; and `COMMIT`. The dump and the counts therefore see the same snapshot,
     even while maintenance and users write.
  2. On the host: `pg_restore --list` must read the dump; then its size and SHA-256;
     `codeatlas-compose run --rm -v /var/lib/codeatlas/backup:/backup:ro api python -m
     codeatlas.ops backup-manifest --counts /backup/counts.json ...` builds `manifest.json` from the
     counts, the running commit, and those values; both files are uploaded to
     `s3://<bucket>/backups/<UTC date>/`.
  3. On success, `BackupCompleted=1`. A failure leaves the metric missing, which fires
     `backup-missing` after 26 hours (R10).
- **Storage**: the bucket blocks public access and uses SSE-S3 encryption. One lifecycle
  configuration (a bucket has only one) holds both rules: `releases/` expires after 30 days and
  `backups/` after 7 days (FR-015). The pilot's and the drill's roles may read under `backups/`;
  only the pilot's may create objects there, and not delete them: its `PutObject` permission
  requires `s3:if-none-match = *`, so `backup.sh` uploads with `aws s3api put-object
  --if-none-match '*'` and no existing backup can be overwritten. A compromised host therefore
  cannot erase or replace the backups. The load test's role cannot read them. Without versioning,
  a backup expires 7 days after it is taken, rounded up to the next midnight UTC, and S3 removes
  it shortly after that.
- **Restore**: `restore.sh <date>` downloads a backup into `/var/lib/codeatlas/backup`, checks its
  SHA-256, stops `api` and `worker`, runs `pg_restore --clean --if-exists --no-owner
  --exit-on-error` into `db`, and then runs `python -m codeatlas.ops verify-restore
  /backup/manifest.json` in the `api` image with the same mount, which compares the restored row
  counts and Alembic revision with the manifest. It leaves `api` and `worker` stopped for the
  operator.
- **Recovery exercise** (SC-008): the developer builds the `drill` environment from
  `infra/stack/` with `drill.tfvars`, releases the pilot's commit to it by hand, restores the latest
  backup, and starts `caddy`, `web`, and `api` without `worker`, so no background job acts on the
  restored data. The drill reads the pilot's parameters, and `APP_ORIGIN` comes from its own
  hostname (R5). The developer adds `https://drill.<domain>/auth/github/callback` as a second
  callback URL of the pilot App, signs in on the drill, and checks their repositories, answers, and
  reviews. The time from `terraform apply` to that check is recorded; then the drill is destroyed.
- **Host loss in the pilot**:
  - Data volume intact (the usual case): `terraform apply -replace=aws_instance.host`. The volume
    attachment stops the old instance before detaching, the new host mounts the same volume, and
    the Elastic IP moves with it. The new host has no bundle or rendered configuration, so the
    developer then runs the release Run Command for the commit in
    `/var/lib/codeatlas/state/running`. Caddy's certificates are kept on the data volume. No
    restore is needed.
  - Data volume lost: remove the lost volume from Terraform state, apply to create a new one,
    release the last commit, restore the latest backup, and start `api` and `worker`. Up to 24
    hours of changes are lost (FR-031). The guide tells the developer to tell pilot users the
    backup time, so they can disconnect repositories again (spec edge case); access losses are
    found again by the daily check (002 FR-012).
  - A change to the bootstrap template is applied the same way as the intact-volume case, because
    the plan shows it as a host replacement (R2).
- **Disclosure** (FR-018): `ExternalProcessingAcceptance` and the README's data section state that
  deleted data can remain in encrypted backups for up to 10 days after deletion: purged within 24
  hours (001 FR-035), then kept in the last backup taken before the purge for 7 days, rounded up to
  the next midnight UTC, until S3 removes it, which happens asynchronously.

**Rationale**: A logical dump is portable to any PostgreSQL 17 (including RDS later), can be
verified by counting rows, and restores into a fresh host. The separate data volume makes the
common host failure a replacement instead of a restore.

**Alternatives considered**:

- **EBS snapshots through Data Lifecycle Manager**: crash-consistent copies that restore fast, but
  only as whole volumes in AWS, and their content cannot be verified without restoring them. A
  second mechanism next to the dump adds no current value.
- **Continuous WAL archiving (pgBackRest or WAL-G)**: data loss of minutes instead of a day, at the
  cost of another tool to run and monitor. Revisit with the RDS condition in ADR 0010.
- **RDS automated backups**: require RDS (R1).
- **Bucket versioning against overwrites**: protects earlier versions, but each expired backup
  then lingers as a noncurrent version, which lengthens the retention the disclosure must state.
- **Counting rows in a separate transaction**: maintenance writes every 10 minutes, so the counts
  would not match the dump, and `verify-restore` would fail spuriously.

## R13. Pilot access list (FR-002 to FR-004, ADR 0013)

**Decision**:

- **Table** `pilot_users`: `github_user_id` (primary key), `github_login`, `note`, and `added_at`.
  It is keyed by GitHub's numeric user ID, because a login can be renamed and then claimed by
  someone else.
- **Enforcement**: `complete_login` checks the list right after GitHub returns the user, before it
  creates a user or workspace or stores tokens. A user not on the list:
  - gets no `users`, `workspaces`, `github_credentials`, or `sessions` row; the token GitHub issued
    is discarded;
  - is redirected to `/?error=not_invited`, where the home page explains that the pilot is by
    invitation (FR-003); and
  - produces a `sign_in` audit event with outcome `denied` and the GitHub login in its detail.
- **When it applies**: always in production (`CODEATLAS_ENV=production`), and elsewhere only with
  `ACCESS_LIST_REQUIRED=1`, which the integration tests set through the `settings` fixture.
  Development, the shared end-to-end stack, and the load test do not use it.
- **Operator commands** (FR-004), run by the developer on the host through Session Manager as
  `codeatlas-compose run --rm api python -m codeatlas.ops pilot-users <command>`:
  - `add <login> [--note]`: resolves the login to its user ID through the GitHub gateway's new
    `get_user_by_login` (`GET /users/{login}`, public data, no credentials; the fake gateway in
    fake mode), and adds the row;
  - `remove <login>`: deletes the row and revokes all of the user's sessions in the same
    transaction, so their next request gets 401 and sign-in is refused;
  - `delete-data <login>`: for a removed user, disconnects every repository in their workspace
    through the existing disconnect (data purged within 24 hours, 001 FR-035), deletes their stored
    GitHub tokens, and revokes their sessions; and
  - `list`.
  Each change records an audit event (`pilot_user_add`, `pilot_user_remove`,
  `pilot_user_delete_data`). No release is needed.
- **Size**: at most 10 users by default (`PILOT_USER_LIMIT`); `add` refuses beyond it.

**Rationale**: A table with commands takes effect at once, survives restarts, is audited, and is
restored with the backups. Checking before any row is written satisfies FR-003 without cleanup.

**Alternatives considered**:

- **An environment variable listing logins**: a restart per change, no audit, and logins can be
  reused after a rename.
- **Membership of a GitHub organization or team**: needs an organization and another App
  permission, and invites people into it.
- **Gating by GitHub App installation**: anyone can install a public App.
- **An admin page**: an admin role and screens for a list the developer edits a few times.

## R14. Pilot-wide daily limits (FR-005, SC-003, ADR 0013)

**Decision**:

- **Table** `pilot_usage_counters`: `usage_date` (primary key, UTC day), `questions_count`, and
  `reviews_count`.
- **Reservation**: `reserve_question` and `reserve_review` first reserve one pilot-wide use with
  the same guarded upsert as the workspace counter (it increments only while below the limit), then
  the workspace use, in the caller's transaction. If either fails, the transaction rolls back both.
  `refund_review` (a review with nothing to review, 003 FR-020) refunds both.
- **Refusal**: `429 pilot_limit_reached`, with `resets_at`, `limit`, and `allowance`, like
  `daily_limit_reached`, and the message "CodeAtlas has reached today's limit of <limit> <questions
  or reviews> for all pilot users." The frontend appends "New questions can be asked after <local
  time>." (or reviews). Browsing and search are unaffected, and accepted jobs finish.
- **Settings**: `PILOT_DAILY_QUESTION_LIMIT=30` and `PILOT_DAILY_REVIEW_LIMIT=15`. The load test and
  the end-to-end tests raise them.
- **Worst-case spend** (SC-003), at the Gemini prices from 2027-01-01 (001 R11: $1.50 input and
  $7.50 output per million tokens), with at most two calls per attempt and one billed attempt
  (transient failures are usually not billed, 003 R7):

  | Request | Per call | Per request (2 calls) | Daily limit | Daily worst case |
  | --- | --- | --- | --- | --- |
  | Question (about 20,000 input, 16,000 output tokens) | $0.15 | $0.30 | 30 | $9.00 |
  | Review (48,000 input, 16,000 output tokens) | $0.19 | $0.38 | 15 | $5.76 |
  | **Total** | | | | **$14.76** |

  Typical requests cost a fifth of that or less (001 R11, 003 R7). Embeddings are negligible.
  Before 2027 the prices are half.
- **Retries**: a job may make up to 3 attempts (001 FR-030), so the theoretical maximum, with every
  attempt fully billed, is three times the total, about $44 a day. Retries follow provider errors
  and timeouts, which are usually not billed, and the limits count requests, not attempts. SC-003
  therefore counts one billed attempt per request, and the measured daily spend in the first month
  (quickstart) checks that assumption.

**Rationale**: A separate one-row-per-day table keeps the existing workspace counter unchanged,
and the guarded upsert already prevents concurrent submissions from exceeding a limit.

**Alternatives considered**:

- **Summing `usage_counters` across workspaces**: two concurrent submissions could both see room;
  it would need a lock.
- **A spending cap at the provider**: Gemini's billing budgets alert but do not stop requests, and
  a hard stop would fail requests already accepted.
- **Limits of 50 questions and 25 reviews**: a worst case near $25 a day, above SC-003.

## R15. Load test (FR-026, SC-011)

**Decision**:

- **Target**: a `loadtest` environment from `infra/stack/` with `loadtest.tfvars`: the same
  instance type, Compose file, and release images as the pilot, with `CODEATLAS_ENV=development`,
  `CODEATLAS_FAKE_EXTERNALS=1`, raised limits, `RATE_LIMIT_PER_MINUTE=0`, `APP_ORIGIN` from its own
  hostname (R5), and no alarms or pilot data. It is destroyed after the run.
- **Driver**: the existing `evals/perf_check.py` (001 SC-007's categories, sign-in included). It
  gains a `--json` option that writes its summary. A `load-test.yml` workflow
  (`workflow_dispatch`: base URL, levels, duration) runs it from a GitHub-hosted runner at 10
  concurrent users, then 20, 40, and 80, stopping at the first level that misses a target or
  returns errors (FR-026).
- **Network**: the workflow first measures the median connection and TLS time to `/healthz` and
  reports it, so the report separates network time from server time. The runner's region is not
  guaranteed, which the report states.
- **Resources**: the host's CPU and memory during each level come from EC2's CPU metric and the
  CloudWatch agent, which the load test environment also runs (R10).
- **Report**: `docs/reports/load-test.md`: the steps and inputs to rerun it (workflow inputs,
  variable file, commit), hardware, configuration, duration, the network baseline, and each level's
  p95 latency and error rate by category, the highest level that meets every target (or "at least
  80" when every level does), and peak CPU and memory. R1 uses the memory figure to decide on
  `t4g.small`.

**Rationale**: `perf_check` already encodes the request mix and fake sign-in that SC-007 was
measured with in 001, so the numbers are comparable. A runner outside AWS needs no credentials and
can be rerun by anyone with the repository.

**Alternatives considered**:

- **k6 or Locust**: a second load script to keep in sync with `perf_check`.
- **The developer's laptop**: its network distance would dominate the 0.5-second view target.
- **The host itself**: the driver would compete with the server for CPU.
- **An EC2 load generator**: closest network, but more infrastructure for a one-off run.

## R16. Evaluation (FR-027, FR-028, SC-012)

**Decision**:

- **Benchmark** (FR-027): Code Review Bench (`withmartian/code-review-benchmark`, MIT license),
  pinned to a commit recorded in the report (`e616e849755441da38f18bf3adba2c9583b03803` was read
  while planning).
  - **Items**: the Sentry (Python) and Cal.com (TypeScript) pull requests of its offline set,
    `offline/golden_comments/sentry.json` and `cal_dot_com.json`, 10 each, with 36 and 41 expected
    findings ("golden comments") written and verified by people. Items carry extra keys
    (`original_url`, `az_comment`), which the runner keeps. Four Sentry items carry data warnings
    in `az_comment` (for example a commit missing from the repository, or a pull request that mixes
    many); the report lists them and gives results with and without them.
  - **Runner** `evals/run_bench_eval.py`: reads the golden comments; resolves each pull request's
    base, head, and merge base through GitHub's REST API, following redirects (`calcom/cal.com` is
    now `calcom/cal.diy`), with a read-only token for the rate limit; downloads both archives from
    codeload into the existing cache; reads them with the review job's `read_tree`; and calls
    `analyze` with the real Gemini model, as `run_review_eval.py` does.
  - **Limits**: both repositories exceed 001 FR-009's limits, so the runner raises the repository
    size limits for its own process and records them in the report. The pilot keeps the defaults.
  - **Export**: the benchmark keys everything by the golden comment's URL, not by title (9 of the
    20 titles differ from the upstream or fork titles). For the tool `codeatlas`, the runner writes
    one review entry per pull request into the benchmark's `results/benchmark_data.json`, and its
    candidates into `candidates.json` for the judge model: one candidate per risk, whose text
    includes the file and line range, because the judge reads only the text. Published tools'
    candidates were extracted from their comments by the benchmark's step 2 with a model;
    CodeAtlas's risks are already one issue each, so step 2 is skipped, and the report says so.
  - **Judge**: the benchmark's deduplication (step 2.5) and judge (step 3) run with
    `--tool codeatlas`, through `uv run --directory <benchmark>/offline` (`offline/` has no build
    system, so `--project` fails, and it resolves `results/` and `.env` from the working
    directory). The judge is an OpenAI-compatible client configured by `MARTIAN_API_KEY`,
    `MARTIAN_BASE_URL`, and `MARTIAN_MODEL`; its results directory is named after the model.
    Judges, in order: Claude Opus 4.5 (`claude-opus-4-5-20251101`, the default judge of the
    benchmark's dashboard, through Anthropic's OpenAI-compatible endpoint), then GPT-5.2 (also
    published, with an OpenAI key). A review during planning reported that Claude Sonnet 4.5, the
    third published judge, was deprecated on 2026-09-30 and retires on 2026-11-30, while a model
    table cached on 2026-09-25 still listed it as active; the developer checks the provider's
    deprecation page before the run, and runs the evaluation early either way. If no published
    judge is served, the report re-judges the compared tools' published candidates with a current
    model and says so (FR-027); that model may need the client's `temperature=0` removed, which the
    report records.
  - **Directories**: the benchmark's steps read and write `results/<MARTIAN_MODEL with "/" replaced
    by "_">/`, while the published results sit in `results/anthropic_claude-opus-4-5-20251101/` and
    `results/openai_gpt-5.2/`. The runner writes CodeAtlas's candidates under the directory the
    steps will use, runs step 3 without `--dedup-groups` (it finds the file itself), and reads the
    published `evaluations.json` from the published directory of the same judge.
  - **Comparison**: from the benchmark's published `evaluations.json` for the same judge, the
    runner sums each tool's true positives, false positives, and false negatives over the same 20
    golden URLs, and the report shows precision and recall side by side.
  - **Cost**: 20 reviews at $0.03 to $0.38 each, and a few dollars of judge calls.
  - **Caveats in the report**: the pull requests come from well-known repositories that models may
    have seen in training; the judge is a model; CodeAtlas reports risks only, so missed style
    findings count against recall; step 2 was skipped for CodeAtlas; the data warnings; and the
    size limits differ from the pilot's.
- **Project sets** (FR-028): `run_qa_eval.py` on `qa_v1.jsonl` and `run_review_eval.py` on
  `review_v2.jsonl` with the real providers, following `backend/evals/README.md`. The report labels
  question-set numbers "measured on a set no person has reviewed yet" and review-set numbers
  "measured on a set reviewed only by a model". The human audits of 001 SC-005 and 003 SC-004 are
  deferred and listed as such.
- **Report**: `docs/reports/evaluation.md`, with denominators, per-item failures, model cost and
  latency per item, and the commit, set versions, and SHA-256 of each set (SC-012).

**Rationale**: Labels written by people answer the main weakness of the project's own review set,
and published results of other tools on the same pull requests give the numbers a reference point.
Reusing the benchmark's judge code keeps CodeAtlas's numbers comparable with the published ones.

**Alternatives considered**:

- **Posting reviews to GitHub forks, as the benchmark's standard flow does**: CodeAtlas does not
  write to GitHub (001 FR-032); the candidates can be exported directly instead.
- **Running step 2 on CodeAtlas's Markdown export**: closer to how other tools were processed, but
  it would let a model re-split risks that are already one issue each.
- **The benchmark's Go, Ruby, and Java pull requests**: CodeAtlas extracts declarations only from
  Python and TypeScript.
- **A judge of our own**: results would not be comparable with the published ones.
- **Human review of the project's sets first**: deferred by the developer.

## R17. Documentation (FR-029 to FR-031)

**Decision**:

- **README**: a GIF of at most 60 seconds at the top showing the main story (connect a repository,
  ask where something is implemented, open the cited code, review a pull request), recorded from the
  pilot and stored as `docs/assets/demo.gif` under 10 MB; an architecture diagram in Mermaid, which
  GitHub renders; a "Known limitations" section (FR-031); a data section with the backup retention;
  and links to both reports and the operations guide.
- **Operations guide** `docs/operations.md`: one-time setup (accounts, domain, state bucket, both
  Terraform modules, parameters, GitHub App, SNS confirmation, GitHub environment), releasing and
  rolling back by hand, restoring, the recovery exercise, host loss, replacing each credential,
  managing pilot users, deleting a removed user's data, running the load test, saved log queries,
  and tearing everything down (FR-030).

**Rationale**: A GIF plays inline on GitHub, while a repository-hosted video does not; Mermaid keeps
the diagram reviewable as text.

**Alternatives considered**: an MP4 (needs an upload outside the repository to play inline); a
diagram image (harder to keep current).

## R18. Tests and checks

**Decision**:

- **Unit tests**: the rate limiter (window, idle eviction, exempt paths, 429 body); production
  settings validation (each missing setting named, no value in the message); the EMF lines; the
  structured log fields and their allow-list; the backup manifest and restore verification
  comparison; the benchmark export and comparison.
- **Integration tests** against PostgreSQL: sign-in by a user not on the list writes no user,
  workspace, credential, or session row and records a denied audit event; `remove` revokes sessions;
  `delete-data` disconnects every repository; pilot-wide limits under concurrent submissions and
  their refund; `/readyz` with the database reachable and unreachable; queue metrics from the
  worker.
- **End-to-end** (Playwright, fake mode): the invitation page at `/?error=not_invited`, and the
  pilot-wide limit message with the API's response intercepted. The sign-in decision itself is
  covered by integration tests, so the shared end-to-end stack keeps the access list off.
- **Test settings**: `RATE_LIMIT_PER_MINUTE=0` and generous pilot-wide limits in
  `backend/tests/conftest.py`, CI's end-to-end configuration, `.env.example`, and `perf_check`'s
  instructions, so existing suites do not hit the new limits. Tests that need other values change
  the cached settings through the existing `settings` fixture, never by editing the environment,
  because `get_settings` is cached and a changed environment would leak into later tests. The rate
  limit test builds its own app with `create_app()`, because the module-level `app` is built at
  import.
- **Host script tests**: `bats` tests in `deploy/tests/` run the host scripts with stub `docker`,
  `psql`, `curl`, `openssl`, and `aws` commands on `PATH`: a failure before the switch (rendering,
  a pull, or the migration) exits 2 without touching the services or `.env`; a failed check rolls
  back and exits 3; a failed check with no previous release exits 4; a successful release updates
  `current`, `.env`, and the state files, and recreates `caddy` only when the Caddyfile changed; a
  missing required parameter makes render-config fail naming it; `restore.sh` refuses a dump
  whose checksum does not match; `backup.sh` publishes no metric when the dump step fails; and
  `check-certificate.sh` computes the days left from an end date.
- **Logging in commands**: `python -m codeatlas.ops` calls `configure_logging()` only under
  `if __name__ == "__main__"`, so tests that call `main([...])` keep their log capture.
- **CI additions**: production builds of both images, `terraform fmt -check` and
  `terraform validate`, `shellcheck`, the `bats` tests, `docker compose -f deploy/compose.yml
  config`, and `caddy validate`.
- **Operational checks**, run by the developer and recorded in the quickstart: the fault tests
  (SC-007), the recovery exercise (SC-008), five releases and a forced rollback (SC-009, SC-010),
  the port scan (SC-005), and the credential scan (SC-006, with `gitleaks` over the history, the
  images, and an export of the logs).

## R19. Monthly cost (SC-002)

| Item | Monthly |
| --- | --- |
| EC2 `t4g.medium`, 730 hours at $0.0336 | $24.53 |
| EBS gp3, 40 GiB at $0.08 | $3.20 |
| Public IPv4 address (Elastic IP), 730 hours at $0.005 | $3.65 |
| Route 53 hosted zone | $0.50 |
| Route 53 health checks, 2 (basic free for AWS endpoints; HTTPS option $1.00 each) | $2.00 |
| CloudWatch (logs under 5 GB, 9 custom metrics, 10 alarms, 1 dashboard: free tier) | $0.00 |
| S3 (7 nightly dumps of about 1 GB, release bundles) | about $0.25 |
| ECR (about 2 GB of image layers) | about $0.20 |
| SSM Parameter Store and Run Command, SNS email, Budgets, IAM | $0.00 |
| Data transfer out (under the 100 GB monthly free allowance) | $0.00 |
| **Total** | **about $34** |

Standard CPU credits avoid surplus-credit charges (R1). If Route 53 classes a check of the domain
as a non-AWS endpoint, each check costs up to $2.75, about $3.50 more in total, still under $40.
The temporary environments cost cents per hour while they exist. Excluded, as in SC-002: model
providers and the domain name (about $10 to $20 a year). A one-year Compute Savings Plan would cut
the instance cost by about a third; it is a commitment, so it is left for after a month of measured
use.
