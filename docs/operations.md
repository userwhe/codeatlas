# Operations guide

How to build, release, and run the CodeAtlas pilot: one EC2 host per environment, running the
Compose services in [`deploy/`](../deploy) behind Caddy, defined by the Terraform modules in
[`infra/`](../infra). The interfaces (environments, Terraform variables, SSM parameters, host layout,
operator commands, and host scripts) are in
[specs/004-pilot-deployment/contracts/operations.md](../specs/004-pilot-deployment/contracts/operations.md),
and the decisions behind them in [research.md](../specs/004-pilot-deployment/research.md) and ADRs
0010 to 0013.

Every command below that writes to AWS, DNS, GitHub, or the pilot's data changes a live system.
Read each Terraform plan before applying it, and each command before running it.

## Conventions

- Commands run from the repository root unless they are marked as running on the host.
- AWS credentials come from IAM Identity Center. Before a session:

  ```bash
  aws sso login --profile codeatlas
  export AWS_PROFILE=codeatlas AWS_REGION=us-east-1
  ```

- Placeholders: `example.dev` is your domain, `codeatlas.example.dev` the pilot's name, and
  `<account ID>` your AWS account ID. Terraform plans are saved as `plan.out` in the module's
  directory; Git ignores them, the real `*.tfvars` files, and `backend.hcl`. Delete a plan once it
  is applied.
- Commands marked "on the host" run in a Session Manager session, which needs no SSH key or open
  port:

  ```bash
  INSTANCE_ID="$(aws ec2 describe-instances \
    --filters Name=tag:codeatlas:environment,Values=pilot Name=instance-state-name,Values=running \
    --query 'Reservations[].Instances[].InstanceId' --output text)"
  aws ssm start-session --target "$INSTANCE_ID"
  ```

## Environments

| Environment | Variable file | State key | Purpose |
| --- | --- | --- | --- |
| `pilot` | `pilot.tfvars` | `stack/pilot.tfstate` | The service; always on |
| `drill` | `drill.tfvars` | `stack/drill.tfstate` | A recovery exercise; reads the pilot's parameters |
| `loadtest` | `loadtest.tfvars` | `stack/loadtest.tfstate` | A load test in fake mode |

Each environment has its own name, state, log groups, and data volume. Only the pilot's data volume
is protected from `terraform destroy`.

## One-time setup

### Tools

- Terraform 1.11 or later.
- The AWS CLI v2 and the Session Manager plugin for it.
- Docker with Buildx. The images are built for `linux/arm64`: natively on an Arm machine such as an
  Apple silicon Mac, and through QEMU emulation elsewhere (slower).
- `jq` and `openssl`, which `deploy/put-secrets.sh` uses.
- For the checks CI runs: `shellcheck` and `bats`.

### AWS access

In the AWS account, enable IAM Identity Center, create your user, and assign it an administrator
permission set. Then configure a profile for `us-east-1`:

```bash
aws configure sso --profile codeatlas
```

### The state bucket

Terraform keeps its state in a versioned bucket, created once:

```bash
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
STATE_BUCKET="codeatlas-tfstate-$ACCOUNT_ID"
aws s3api create-bucket --bucket "$STATE_BUCKET" --region us-east-1
aws s3api put-bucket-versioning --bucket "$STATE_BUCKET" \
  --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket "$STATE_BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "$STATE_BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

### Shared resources

`infra/shared/` holds the image repositories, the bucket for release bundles and backups, the hosted
zone, the alert topic `codeatlas-alerts` with an email subscription, and the monthly budget
`codeatlas-monthly`. Copy the examples and fill them in: the account ID in `backend.hcl`; the
domain, the alert email address, the monthly budget, and the repository's OIDC subject prefix in
`shared.tfvars` (the release workflow uses the prefix later, in ["Setting up
releases"](#setting-up-releases), but the variable is required now).

```bash
cp infra/shared/backend.hcl.example infra/shared/backend.hcl
cp infra/shared/shared.tfvars.example infra/shared/shared.tfvars
gh api repos/<owner>/<repo>/actions/oidc/customization/sub --jq .sub_claim_prefix   # the OIDC subject prefix

terraform -chdir=infra/shared init -backend-config=backend.hcl
terraform -chdir=infra/shared plan -var-file=shared.tfvars -out=plan.out
terraform -chdir=infra/shared show plan.out    # review
terraform -chdir=infra/shared apply plan.out
```

### Confirming the alert subscription

Applying `infra/shared` subscribes the alert email address to `codeatlas-alerts`, and AWS sends it
a message titled "AWS Notification - Subscription Confirmation". Open its "Confirm subscription"
link; until then, no alarm or failed release reaches you. Check, and send a test message:

```bash
TOPIC_ARN="$(terraform -chdir=infra/shared output -raw alert_topic_arn)"
aws sns list-subscriptions-by-topic --topic-arn "$TOPIC_ARN" \
  --query 'Subscriptions[].SubscriptionArn' --output text    # an ARN, not PendingConfirmation
aws sns publish --topic-arn "$TOPIC_ARN" --subject "CodeAtlas test" --message "Test notification."
```

Every notification email ends with an unsubscribe link; following it stops all alerts. To
subscribe again, or after changing `alert_email`, apply `infra/shared` again and confirm the new
message. The budget's emails go to the same address directly and need no confirmation: once each
month at 80% and 100% of actual spend and at 80% of forecast spend (forecasts start after about five
weeks of data).

### Delegating the domain

At the domain's registrar, replace the name servers with the hosted zone's:

```bash
terraform -chdir=infra/shared output name_servers
dig +short NS example.dev    # repeat until it lists the same four servers
```

Caddy obtains the pilot's certificate from Let's Encrypt at the first release, so the delegation
must be in effect by then.

### The pilot GitHub App

The pilot uses its own App, separate from the development App. In GitHub, open Settings, then
Developer settings, then GitHub Apps, then New GitHub App, and set:

- **GitHub App name**: for example `CodeAtlas`; **Homepage URL**: `https://codeatlas.example.dev`.
- **Callback URL**: `https://codeatlas.example.dev/auth/github/callback`.
- **Expire user authorization tokens**: on.
- **Webhook**: active, with the URL `https://codeatlas.example.dev/webhooks/github` and a secret
  generated with `openssl rand -hex 32`. Keep the secret for `put-secrets.sh` below.
- **Repository permissions**: Contents, Metadata, and Pull requests, each `Read-only`. No other
  permissions.
- **Events**: subscribe to Push. Installation and Installation repositories events reach every App
  without a subscription.
- **Where can this GitHub App be installed**: any account, so pilot users can install it.

After creating it, note the App ID, the client ID, and the slug (the last part of the App's public
page URL), generate a client secret, and generate a private key, which downloads a `.pem` file.

### The pilot's parameters

`deploy/put-secrets.sh` writes the pilot's SSM parameters under `/codeatlas/pilot/`: the mode
settings, a generated token encryption key and database password, and the values it prompts for
(secrets are not shown while typed):

```bash
deploy/put-secrets.sh pilot --github-app-private-key <path to the downloaded .pem file>
```

Values never appear on a command line or in the output. Existing parameters are kept, so running it
again only adds what is missing. Afterward, delete the downloaded key file or move it to offline
storage.

### The pilot stack

`infra/stack/` builds one environment. The state key is given at `init`, matching the variable
file; `-reconfigure` switches between environments. Always initialize with the key of the variable
file you plan with.

```bash
cp infra/stack/backend.hcl.example infra/stack/backend.hcl
cp infra/stack/pilot.tfvars.example infra/stack/pilot.tfvars

terraform -chdir=infra/stack init -reconfigure -backend-config=backend.hcl \
  -backend-config=key=stack/pilot.tfstate
terraform -chdir=infra/stack plan -var-file=pilot.tfvars -out=plan.out
terraform -chdir=infra/stack show plan.out    # review
terraform -chdir=infra/stack apply plan.out
```

The host installs Docker, the AWS CLI, and the CloudWatch agent, and mounts its data volume on first
boot; in the pilot it also installs the backup and certificate check timers. Wait for it to finish
(on the host):

```bash
sudo cloud-init status --wait    # "status: done"
```

Then release the current `main` commit by hand (["Releasing by hand"](#releasing-by-hand)) and add
the pilot users (["Managing pilot users"](#managing-pilot-users)), starting with yourself.

### Setting up releases

Once the pilot runs, releases go through the `Release` workflow (["Releases"](#releases)). It needs
the release role from `infra/shared`, the GitHub environment `pilot`, and six repository variables.
GitHub holds no AWS key: the workflow's `release` job gets short-lived credentials through GitHub's
OIDC provider for the role `codeatlas-release`, which trusts only this repository's jobs in the
`pilot` environment.

1. Read the repository's OIDC subject prefix:

   ```bash
   gh api repos/<owner>/<repo>/actions/oidc/customization/sub --jq .sub_claim_prefix
   ```

   The repository uses GitHub's immutable subject format, so the prefix has the form
   `repo:<owner>@<owner ID>/<repo>@<repo ID>`. Set `github_oidc_subject_prefix` to it in
   `infra/shared/shared.tfvars`. Transferring or recreating the repository changes the IDs, and so
   the prefix; a wrong prefix makes the release job fail with "Not authorized to perform
   sts:AssumeRoleWithWebIdentity".
2. Apply `infra/shared` as in ["Shared resources"](#shared-resources). The plan adds the OIDC
   provider for `token.actions.githubusercontent.com`, the role `codeatlas-release`, and its policy:
   pushing to the two image repositories, uploading under `releases/` in the bucket, Run Command on
   instances tagged `codeatlas:environment=pilot`, reading the command's results, and publishing to
   `codeatlas-alerts`. An account has one provider per URL; if yours already has GitHub's, import it
   before planning:

   ```bash
   terraform -chdir=infra/shared import -var-file=shared.tfvars aws_iam_openid_connect_provider.github \
     "arn:aws:iam::<account ID>:oidc-provider/token.actions.githubusercontent.com"
   ```

3. Create the environment `pilot` (repository Settings, Environments, New environment). Under
   "Deployment protection rules", check "Required reviewers" and add yourself; leave "Prevent
   self-review" off, since you approve the releases of your own merges. Under "Deployment branches
   and tags", choose "Selected branches and tags" and add the branch `main`. Add no secrets. Or from
   the command line:

   ```bash
   gh api -X PUT repos/<owner>/<repo>/environments/pilot --input - <<EOF
   {"reviewers": [{"type": "User", "id": $(gh api user --jq .id)}],
    "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
   EOF
   gh api -X POST repos/<owner>/<repo>/environments/pilot/deployment-branch-policies \
     -f name=main -f type=branch
   ```

4. Set the repository variables (Settings, Secrets and variables, Actions, Variables). They are
   settings, not secrets:

   ```bash
   gh variable set AWS_REGION --body us-east-1
   gh variable set RELEASE_ROLE_ARN --body "$(terraform -chdir=infra/shared output -raw release_role_arn)"
   gh variable set RELEASE_BUCKET --body "$(terraform -chdir=infra/shared output -raw bucket)"
   gh variable set ALERT_TOPIC_ARN --body "$(terraform -chdir=infra/shared output -raw alert_topic_arn)"
   gh variable set ECR_REGISTRY --body "$(terraform -chdir=infra/shared output -json ecr_repositories |
     jq -r '."codeatlas/api" | split("/")[0]')"
   gh variable set PILOT_HOSTNAME --body codeatlas.example.dev
   ```

5. Check that GitHub holds no AWS key, in the repository or the environment (quickstart scenario
   18):

   ```bash
   gh secret list
   gh secret list --env pilot
   ```

## Releases

Every push to `main` whose CI run succeeds starts the `Release` workflow
(`.github/workflows/release.yml`) for that commit. Nothing is written to AWS until you approve it:

1. `build` builds both images for the commit on Arm runners, without cloud credentials, and keeps
   them as workflow artifacts for 3 days.
2. `summary` writes the release summary on the run's Summary page: first the schema changes (every
   file the commit adds under `backend/alembic/versions/` since the running version, with the
   expand-then-contract reminder), then the commit, the commit the pilot runs (from `/version`),
   and the commits in between. On a first release, or when the pilot does not answer, the running
   version is unknown.
3. `release` waits in the `pilot` environment. Read the summary, then choose "Review deployments",
   select `pilot`, and approve. The environment's deployment history records the commit, who
   approved it, when, and the outcome.

After approval, the `release` job assumes `codeatlas-release`; pushes `codeatlas/api:<sha>` and
`codeatlas/web:<sha>` unless the tag exists; uploads `deploy/` as `releases/<sha>/deploy.tar.gz`;
and sends the Run Command that extracts the bundle on the instance tagged
`codeatlas:environment=pilot` and runs its `release.sh <sha>`, as in ["Releasing by
hand"](#releasing-by-hand). Meanwhile it probes `https://codeatlas.example.dev/readyz` and `/`
once a second, and waits up to 20 minutes for the command to end. It writes the outcome and the
longest outage (the longest run of failed probes) to the Summary page, and on any failure emails
the commit and the outcome through `codeatlas-alerts`.

`release.sh` checks the new release for up to 120 seconds through Caddy: `/readyz` and `/` must
return 200, and `/version` must report the commit. Its exit status is the outcome:

| Exit status | Meaning | What to do |
| --- | --- | --- |
| 0 | Released and checked | Nothing |
| 2 | Failed before the switch: configuration, registry login, pull, or migration. The previous release still serves, untouched | Read the output, which names the failing step; fix it and release again |
| 3 | The check failed (or a step after the switch did), and the previous release serves again. The database keeps the new release's migrations | Read the output: its "the check failed" line shows what `/readyz`, `/`, and `/version` returned |
| 4 | The previous release did not become ready either, or the host had none to return to (the new `api`, `worker`, and `web` are then stopped) | The pilot is down: look at the services (on the host: `sudo codeatlas-compose ps`, `sudo codeatlas-compose logs --tail 100 api`), then fix forward or release a known good commit |

The host's output is in the log group `/codeatlas/pilot/releases`, and each outcome is a line in
`/var/log/codeatlas/releases.log` (shipped to `/codeatlas/pilot/host`):

```bash
aws logs tail /codeatlas/pilot/releases --since 1h
```

To release another commit of `main`, or the same commit again, dispatch the workflow from `main`
(Actions, Release, Run workflow), or:

```bash
gh workflow run release.yml --ref main -f commit=<full SHA>
```

A dispatched commit must be on `main` with a successful CI run; otherwise `summary` fails, and
nothing waits for approval.

### Rejecting a stale run

One release job runs at a time (the concurrency group `release-pilot`). A run waiting for approval
holds the group, so newer runs queue behind it, and of those GitHub keeps only the newest. When
several merges land before you approve, release only the newest: reject each older run that waits
(its "Review deployments", `pilot`, Reject). The newest run then waits for your approval. A
rejected run writes nothing and sends no alert. From the command line:

```bash
gh run list --workflow release.yml --status waiting
ENVIRONMENT_ID="$(gh api repos/<owner>/<repo>/environments/pilot --jq .id)"
gh api -X POST repos/<owner>/<repo>/actions/runs/<run ID>/pending_deployments \
  -F "environment_ids[]=$ENVIRONMENT_ID" -f state=rejected -f comment="Superseded by a newer commit"
```

### Migrations: expand, then contract

A release migrates the database before it switches, and a rollback restores the previous images,
not the database. Every migration must therefore keep the previous release working:

- **Expand** in one release: add tables, nullable columns or columns with a server default, and
  indexes. The previous release ignores what it does not know.
- **Contract** in a later release, once neither the running release nor `state/previous` uses the
  old shape: drop the old column or table, or tighten a constraint.
- Never drop, rename, or change the type of a column in the release that stops using it. A rename
  takes three releases: add the new column and write both (with a backfill); read only the new one;
  drop the old one.

The summary lists a release's new migrations first. Before approving one, check it against these
rules.

### Rolling back

A failed check rolls back by itself (exit status 3). To leave a release whose check passed but which
misbehaves:

- **No migration since the previous release**: release the previous commit. Its bundle is on the
  host (on the host):

  ```bash
  PREVIOUS="$(cat /var/lib/codeatlas/state/previous)"
  sudo /opt/codeatlas/releases/$PREVIOUS/release.sh "$PREVIOUS"
  ```

  Or dispatch the workflow with that commit; its summary warns that the pilot runs a newer one.
- **The release added a migration**: releasing the previous commit stops at its migration with exit
  status 2 and changes nothing, because the previous image's Alembic does not know the database's
  newer revision. Instead, release the running commit again with a check that cannot pass. It
  starts the previous release without migrating, as an automatic rollback does, and exits 3 (on the
  host):

  ```bash
  RUNNING="$(cat /var/lib/codeatlas/state/running)"
  sudo /opt/codeatlas/releases/$RUNNING/release.sh "$RUNNING" --expect-version rollback
  ```

  Afterward `state/running` names the previous commit and `state/previous` the one you left, so
  releasing `state/previous` later goes forward again. Through the workflow, this is the rehearsal
  below, with an alert at the end.

Both need the previous release's bundle on the host; a replaced host has only the bundles released
to it.

### Rehearsing a rollback

The forced rollback (SC-010, quickstart scenario 14) proves that a failed check restores the
previous release. First check that the host has a previous release to return to: on the host,
`cat /var/lib/codeatlas/state/previous` names a commit whose bundle is in
`/opt/codeatlas/releases/`. Without one, the rehearsal stops the pilot (exit status 4). Then:

```bash
gh workflow run release.yml --ref main -f expect_version=wrong
```

Approve the run, and note the time. It releases the head of `main` (again, if the pilot runs it),
the check fails because `/version` does not report `wrong`, and `release.sh` starts the previous
release and exits 3. Expect the job to fail, an alert email, `/version` reporting the previous
commit within 5 minutes of the approval, and the longest outage on the Summary page. Record the
times in the quickstart's validation record, then release the newest commit again (dispatch without
`expect_version`).

## Releasing by hand

The pilot normally releases through the workflow (["Releases"](#releases)). The first release, the
drill, the load test, and a replaced host use these steps. A release puts one commit on a host: both
images tagged with the full commit SHA in ECR, the commit's `deploy/` directory as a bundle in S3,
and a Run Command that runs the bundle's `release.sh`. The script renders the configuration, pulls
the images, migrates, switches to the new release, checks `/readyz`, `/`, and `/version` through
Caddy, and rolls back if the check fails.

Set the commit and the environment (the drill and the load test use their own names):

```bash
git fetch origin
SHA="$(git rev-parse origin/main)"    # any commit, as a full 40-character SHA
ENVIRONMENT=pilot
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
BUCKET="codeatlas-$ACCOUNT_ID-$AWS_REGION"
```

### 1. Images

Tags are immutable, so build and push each image only if its tag does not exist yet. Build from a
clean checkout of the commit:

```bash
git worktree add --detach ../codeatlas-release "$SHA"
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"

if ! aws ecr describe-images --repository-name codeatlas/api --image-ids imageTag="$SHA" >/dev/null 2>&1; then
  docker buildx build --platform linux/arm64 --build-arg CODEATLAS_RELEASE="$SHA" \
    --tag "$REGISTRY/codeatlas/api:$SHA" --push ../codeatlas-release/backend
fi
if ! aws ecr describe-images --repository-name codeatlas/web --image-ids imageTag="$SHA" >/dev/null 2>&1; then
  docker buildx build --platform linux/arm64 --target production \
    --build-arg NEXT_PUBLIC_RELEASE="$SHA" \
    --tag "$REGISTRY/codeatlas/web:$SHA" --push ../codeatlas-release/frontend
fi
git worktree remove ../codeatlas-release
```

### 2. Bundle

Bundles expire after 30 days, so upload the commit's `deploy/` directory unless it is there.
`git archive` takes exactly the committed files, with their executable bits:

```bash
if ! aws s3api head-object --bucket "$BUCKET" --key "releases/$SHA/deploy.tar.gz" >/dev/null 2>&1; then
  git archive --format=tar.gz -o deploy.tar.gz "$SHA:deploy"
  aws s3 cp deploy.tar.gz "s3://$BUCKET/releases/$SHA/deploy.tar.gz"
  rm deploy.tar.gz
fi
```

### 3. Release

Send the Run Command that extracts the bundle into `/opt/codeatlas/releases/<sha>/` and runs its
`release.sh`, with output in the environment's `releases` log group:

```bash
INSTANCE_ID="$(aws ec2 describe-instances \
  --filters "Name=tag:codeatlas:environment,Values=$ENVIRONMENT" Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].InstanceId' --output text)"

COMMAND_ID="$(aws ssm send-command \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --comment "release $SHA" \
  --cloud-watch-output-config "CloudWatchOutputEnabled=true,CloudWatchLogGroupName=/codeatlas/$ENVIRONMENT/releases" \
  --parameters "$(jq -n --arg sha "$SHA" '{
    executionTimeout: ["1200"],
    commands: [
      "set -eu",
      ". /opt/codeatlas/environment",
      "bundle=/opt/codeatlas/releases/\($sha)",
      "mkdir -p \"$bundle\"",
      "aws s3 cp --region \"$AWS_REGION\" \"s3://$BUCKET/releases/\($sha)/deploy.tar.gz\" - | tar -xz -C \"$bundle\"",
      "\"$bundle/release.sh\" \($sha)"
    ]}')" \
  --query Command.CommandId --output text)"

aws logs tail "/codeatlas/$ENVIRONMENT/releases" --since 10m --follow    # Ctrl-C when it ends
aws ssm get-command-invocation --command-id "$COMMAND_ID" --instance-id "$INSTANCE_ID" \
  --query '[Status,ResponseCode]' --output text
curl -s https://codeatlas.example.dev/version
```

`release.sh` exits 0 when the release passed its check: `/version` reports the commit, and the
footer shows its first seven characters. The other exit statuses (2, 3, and 4) are in the table
under ["Releases"](#releases); `--expect-version V` makes the check expect `V` instead of the commit
(["Rehearsing a rollback"](#rehearsing-a-rollback)). To roll back by hand, see ["Rolling
back"](#rolling-back).

`/opt/codeatlas/releases/` keeps every bundle released on the host; remove old ones by hand when
the disk needs it, never the ones named in `state/running` and `state/previous`.

### Releasing an existing commit

The drill, the load test, and a replaced host use the same steps for a commit that was released
before: set `ENVIRONMENT` to the host's environment, skip the image builds (the tags exist), upload
the bundle again only if it expired, and send the Run Command to that host. A replaced pilot host
takes the commit recorded on its data volume, in `/var/lib/codeatlas/state/running`; `/version`
shows it too while the old host still answers. For the pilot, dispatching the workflow with the
commit (["Releases"](#releases)) does the same after your approval.

## Temporary settings

The optional limits and switches (`DAILY_QUESTION_LIMIT`, `DAILY_REVIEW_LIMIT`,
`PILOT_DAILY_QUESTION_LIMIT`, `PILOT_DAILY_REVIEW_LIMIT`, `RATE_LIMIT_PER_MINUTE`, and
`EMIT_METRICS`) change through parameters and take effect at the next release:

```bash
deploy/put-secrets.sh pilot --set PILOT_DAILY_QUESTION_LIMIT=2
```

Then refresh the running release's configuration (on the host):

```bash
RUNNING="$(cat /var/lib/codeatlas/state/running)"
sudo /opt/codeatlas/current/release.sh "$RUNNING" --refresh-config
```

It renders the new configuration and recreates `api` and `worker`, which read these settings
(["Replacing a credential"](#replacing-a-credential) has the details). Releasing the running commit
again also works, more slowly. To return to the default, remove the setting and refresh again:

```bash
deploy/put-secrets.sh pilot --unset PILOT_DAILY_QUESTION_LIMIT
```

## Managing pilot users

Only GitHub accounts on the access list can sign in; at most 10. The operator commands run in a
one-off `api` container (on the host):

```bash
sudo codeatlas-compose run --rm api python -m codeatlas.ops pilot-users list
sudo codeatlas-compose run --rm api python -m codeatlas.ops pilot-users add <login> --note "<who and why>"
sudo codeatlas-compose run --rm api python -m codeatlas.ops pilot-users remove <login>
```

- `add` resolves the login to its GitHub user ID, so a later rename keeps the access. It exits 1
  if the login does not exist, is already listed, or the list is full.
- `remove` deletes the entry and signs the user out everywhere at once; their next request gets
  the signed-out page, and signing in again shows the invitation page. It exits 1 if the login is
  not listed.
- `list` prints each login, user ID, note, and the date it was added.

Each change is recorded in the audit log.

### Deleting a removed user's data

When a removed user asks for their data to be deleted (on the host):

```bash
sudo codeatlas-compose run --rm api python -m codeatlas.ops pilot-users delete-data <login>
```

It disconnects every repository of the user's workspace (hidden at once and purged within 24
hours), deletes their stored GitHub tokens, and revokes their sessions. It exits 1 while the login
is still listed (remove it first) or when no such user exists.

## Replacing a credential

Replace the parameter (on your machine), then refresh the running release's configuration (on the
host):

```bash
deploy/put-secrets.sh pilot --overwrite <NAME>
```

```bash
RUNNING="$(cat /var/lib/codeatlas/state/running)"
sudo /opt/codeatlas/current/release.sh "$RUNNING" --refresh-config
```

`--refresh-config` renders the configuration from the parameters again and recreates `api` and
`worker` (a restart would keep their old environment and key file), then runs the release check.
It pulls and migrates nothing, changes neither `.env` nor `current`, and accepts only the running
commit. It exits 0 when the check passes; 2 when rendering fails, for example on a missing
parameter, with nothing recreated; and 4 when the check fails afterward. There is nothing to roll
back to then, because the previous configuration is gone: fix the parameter and refresh again.

| Credential | Steps |
| --- | --- |
| `GEMINI_API_KEY`, `VOYAGE_API_KEY` | Create a new key in the provider's console, overwrite the parameter, refresh, then revoke the old key |
| `GITHUB_APP_CLIENT_SECRET` | Generate a new client secret in the App's settings, overwrite, refresh, then delete the old secret |
| `GITHUB_APP_PRIVATE_KEY` | Generate a new private key in the App's settings, run `put-secrets.sh pilot --overwrite GITHUB_APP_PRIVATE_KEY --github-app-private-key <file>`, refresh, then delete the old key in the App's settings and the downloaded file |
| `GITHUB_WEBHOOK_SECRET` | Generate one with `openssl rand -hex 32`, overwrite, refresh, and set it in the App's webhook settings right away. Deliveries in between fail their signature check; redeliver them from the App's Recent Deliveries page, or let the daily check find the missed pushes |
| `TOKEN_ENCRYPTION_KEY` | `put-secrets.sh pilot --overwrite TOKEN_ENCRYPTION_KEY` generates a new key; refresh. The stored GitHub tokens become unreadable, so every user is asked to sign in again; their repositories stay connected |

### Replacing the database password

PostgreSQL reads `POSTGRES_PASSWORD` only when it creates its data directory, so the password
changes in the database first, then in the parameter, then in the running services. `put-secrets.sh`
refuses to overwrite it for that reason. Do the three steps back to back: until the refresh, new
connections from `api` and `worker` fail.

1. Generate a new password on your machine with `openssl rand -hex 24` (only letters and digits:
   it becomes part of `DATABASE_URL`).
2. Change it in the database (on the host). `\password` runs `ALTER ROLE codeatlas PASSWORD '...'`
   with the password already hashed, so it never appears in a log or the shell history:

   ```bash
   sudo codeatlas-compose exec db psql -U codeatlas -d codeatlas
   ```

   ```text
   codeatlas=# \password codeatlas
   Enter new password for user "codeatlas":
   Enter it again:
   codeatlas=# \q
   ```

3. Write the parameter, through a file so the password stays off the command line:

   ```bash
   umask 077
   PASSWORD_FILE="$(mktemp)"
   read -rs -p "New database password: " NEW_PASSWORD && printf '%s' "$NEW_PASSWORD" >"$PASSWORD_FILE"
   unset NEW_PASSWORD
   aws ssm put-parameter --name /codeatlas/pilot/POSTGRES_PASSWORD --type SecureString \
     --overwrite --value "file://$PASSWORD_FILE"
   rm -f "$PASSWORD_FILE"
   ```

4. Refresh the configuration (`release.sh "$RUNNING" --refresh-config`, above). It writes the new
   `DATABASE_URL` and recreates `api` and `worker`; `db` needs no restart, because PostgreSQL reads
   `POSTGRES_PASSWORD` only when it creates its data directory.

## Monitoring

The pilot reports to CloudWatch (research R10): the services' logs, the host scripts' log files
(shipped by the CloudWatch agent), the worker's metrics, the agent's memory and disk metrics, and
two Route 53 health checks. The drill and the load test have logs and agent metrics, but no alarms,
dashboard, or health checks.

### Alerts

Each alarm emails `codeatlas-alerts` when it fires and again when it recovers. The budget emails
separately (["Confirming the alert subscription"](#confirming-the-alert-subscription)).

| Alarm | Fires when | Look first at (on the host unless noted) |
| --- | --- | --- |
| `site-down` | `https://codeatlas.example.dev/readyz` has failed for 5 minutes: the API, its database, or Caddy is down | `sudo codeatlas-compose ps`, then `sudo codeatlas-compose logs --tail 100 api db caddy` |
| `web-down` | `https://codeatlas.example.dev/` has failed for 5 minutes | `sudo codeatlas-compose ps`, then the `web` and `caddy` logs |
| `worker-down` | The worker has sent no heartbeat for 5 minutes | `sudo codeatlas-compose logs --tail 100 worker`; `sudo codeatlas-compose up -d worker` |
| `jobs-waiting` | A job that could run has waited 30 minutes | The dashboard's queue graph and the worker's log: a stuck job, or more work than one worker handles |
| `jobs-failing` | 3 or more jobs failed in an hour (retries used up, timeouts, internal errors) | The saved query "Failed jobs", then the job's lines; the dashboard's provider errors |
| `root-disk-full` | The root volume is more than 80% full | `sudo docker system df`; remove old bundles in `/opt/codeatlas/releases/` (never those in `state/running` and `state/previous`) and unused images with `sudo docker image prune -a` (a rollback then pulls its images again) |
| `data-disk-full` | The data volume is more than 80% full | `sudo du -sh /var/lib/codeatlas/*`; grow the volume (below) |
| `backup-missing` | No backup has completed for 26 hours | ["Backups"](#backups) |
| `certificate-expiring` | The certificate expires within 14 days, or has not been checked for 26 hours | `sudo tail /var/log/codeatlas/check-certificate.log`, then Caddy's log for renewal errors |
| `host-status-check` | EC2 status checks have failed for 2 minutes | The instance's status checks in the EC2 console. Automatic recovery restarts it on new hardware after a system check failure; for an instance check failure, reboot it, and replace the host (["Replacing the host"](#replacing-the-host)) if that does not help |

To grow the data volume, raise `data_volume_gib` in `pilot.tfvars`, plan and apply (the volume
changes in place, without a new host), then extend its file system (on the host):

```bash
sudo resize2fs "$(findmnt -n -o SOURCE /var/lib/codeatlas)"
```

### The dashboard

The CloudWatch dashboard `codeatlas-pilot` (CloudWatch console, Dashboards) shows the last 7 days:
the alarms' states; the health checks; the worker's heartbeat, queued jobs, and oldest runnable job;
CPU, memory, and both volumes; failed jobs, backups, status checks, and the certificate's days left;
and, from the logs, requests and p95 latency by route group (searches, views, submissions, other),
5xx responses, jobs by kind and outcome (`retry_wait` counts the retries), and model provider
errors by provider. The log widgets run Logs Insights queries whenever the dashboard loads.

### Logs

Each environment's log groups keep 7 days: `/codeatlas/<environment>/caddy`, `web`, `api`,
`worker`, `db`, `releases` (Run Command output of releases), and `host` (the files in
`/var/log/codeatlas/`: `backup.log`, `restore.log`, `check-certificate.log`, and `releases.log`,
one stream per file). API and worker lines are JSON with `request_id`, `job_id`, `run_id`,
`snapshot_id`, and, on `request` lines, `method`, `route`, `status`, and `duration_ms`; on
`job_finished` lines, `kind`, `outcome`, `attempt`, and `duration_ms`; on provider errors,
`provider`.

Logs Insights (CloudWatch console, Logs Insights, saved queries) has three queries for the pilot,
under `codeatlas-pilot/`:

- **Lines for a request or job ID**: every line, in every log group, that contains an ID. Replace
  `REQUEST_OR_JOB_ID` with a request ID (every API response has an `X-Request-ID` header, and error
  bodies repeat it as `request_id`) or a job ID. Caddy's lines hold the request ID in their logged
  response headers.
- **p95 latency by route**: request count and p95 `duration_ms` by method and route template.
- **Failed jobs**: the `job_finished` lines with outcome `failed`, with kind, attempt, and job ID.

The same search from the command line, over the last hour:

```bash
ID=<request or job ID>
QUERY_ID="$(aws logs start-query \
  --log-group-names /codeatlas/pilot/caddy /codeatlas/pilot/api /codeatlas/pilot/worker \
  --start-time "$(( $(date +%s) - 3600 ))" --end-time "$(date +%s)" \
  --query-string "fields @timestamp, @log, @message | filter @message like \"$ID\" | sort @timestamp asc" \
  --query queryId --output text)"
sleep 5
aws logs get-query-results --query-id "$QUERY_ID" --output text
```

The CloudWatch agent's own log is `/opt/aws/amazon-cloudwatch-agent/logs/amazon-cloudwatch-agent.log`
(on the host); `systemctl status amazon-cloudwatch-agent` shows whether it runs.

## Backups

Every night at 03:30 UTC, `codeatlas-backup.timer` (pilot only) runs
`/opt/codeatlas/current/backup.sh`. In one database snapshot it dumps the database with `pg_dump`
and counts the rows of every table, so the counts describe exactly the dump; it checks that
`pg_restore` can read the dump, builds `manifest.json` (commit, Alembic revision, the dump's size
and SHA-256, and the row counts), uploads both files, and publishes the `BackupCompleted` metric.
Any failure stops it before the metric, and `backup-missing` fires within about an hour once 26
hours have passed without a successful backup.

- **Where**: `s3://codeatlas-<account ID>-us-east-1/backups/<UTC date>/codeatlas.dump` and
  `manifest.json`, encrypted by S3. They expire 7 days after they are taken (rounded up to the next
  midnight UTC); deleted data can therefore remain in a backup for up to 10 days after deletion,
  which the external processing disclosure states.
- **Protection**: the pilot's role can only create objects under `backups/`, with
  `If-None-Match: *`; it can neither overwrite nor delete a backup. A second backup on the same UTC
  date therefore fails, and so does a retry after a partial upload: the next night's backup is the
  retry.
- **Staging**: the dump is written to `/var/lib/codeatlas/backup/` on the data volume and deleted
  after the upload, so the data volume needs free space for one dump.

Check the last runs and the next one (on the host):

```bash
systemctl list-timers 'codeatlas-*'
sudo tail -n 20 /var/log/codeatlas/backup.log
sudo journalctl -u codeatlas-backup.service --since yesterday
```

List the backups and read a manifest (on your machine; the host's role cannot list the bucket):

```bash
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="codeatlas-$ACCOUNT_ID-$AWS_REGION"
aws s3 ls "s3://$BUCKET/backups/"
aws s3 cp "s3://$BUCKET/backups/<date>/manifest.json" - | jq '{created_at, release, alembic_revision}'
```

Run a backup now, for example after fixing a failure (on the host); `systemctl start` waits until it
ends:

```bash
sudo systemctl start codeatlas-backup.service
sudo tail -n 5 /var/log/codeatlas/backup.log
```

## Restoring a backup

`restore.sh` replaces the host's database with one backup. It downloads the dump and its manifest
into `/var/lib/codeatlas/backup/`, checks the dump's SHA-256 against the manifest (changing nothing
if it differs), stops `api` and `worker`, runs `pg_restore --clean --if-exists --no-owner
--exit-on-error`, and runs `verify-restore`, which compares the restored Alembic revision and row
counts with the manifest. It leaves `api` and `worker` stopped, so you can look before anything
runs. Everything written to the database after the backup is lost, so restore into the running
pilot only when its data is gone.

The host must run the backup's commit (`release` in the manifest), so that the schema matches;
release it first ("Releasing an existing commit") if needed. The script warns when the host runs
another commit. Then (on the host):

```bash
sudo /opt/codeatlas/current/restore.sh <UTC date, for example 2026-10-07>
sudo codeatlas-compose up -d api worker    # after checking; the drill starts only api
```

It exits 0 when the restore is verified; 1 when the download, the checksum, `pg_restore`, or the
verification fails (the output and `/var/log/codeatlas/restore.log` name the step and, for the
verification, each table that differs); 2 for a malformed date. The pilot's and the drill's roles
can read the backups; the load test's cannot.

## Recovery exercise

The drill rebuilds the pilot from its definition and its latest backup, to prove recovery works
and to time it (SC-008: under 2 hours, losing only changes after the backup). The drill reads the
pilot's parameters, has its own name, `drill.example.dev`, and never runs the worker, so no
background job acts on the restored data. Record the start time, then:

1. Build the drill:

   ```bash
   cp infra/stack/drill.tfvars.example infra/stack/drill.tfvars    # set domain and hostname
   terraform -chdir=infra/stack init -reconfigure -backend-config=backend.hcl \
     -backend-config=key=stack/drill.tfstate
   terraform -chdir=infra/stack plan -var-file=drill.tfvars -out=plan.out
   terraform -chdir=infra/stack show plan.out    # review
   terraform -chdir=infra/stack apply plan.out
   ```

   Then wait for its bootstrap (`sudo cloud-init status --wait` in a session on the instance tagged
   `codeatlas:environment=drill`).
2. In the pilot GitHub App's settings (General, Callback URL, "Add Callback URL"), add
   `https://drill.example.dev/auth/github/callback`, so that you can sign in on the drill.
3. Find the latest backup and its manifest's `release` (["Backups"](#backups)): the commit the pilot
   ran when the backup was taken, usually the one `https://codeatlas.example.dev/version` reports.
   Release that commit to the drill, following ["Releasing an existing
   commit"](#releasing-an-existing-commit) with `ENVIRONMENT=drill`. The release starts every
   service on an empty database, the worker too; the restore stops it.
4. Restore the backup, then start the API without the worker (on the drill host):

   ```bash
   sudo /opt/codeatlas/current/restore.sh <latest date>
   sudo codeatlas-compose up -d api
   ```

5. Sign in at `https://drill.example.dev`, and check your repositories, an earlier answer, and an
   earlier review. Do not submit new questions or reviews: without the worker they only queue.
6. Record the end time and the result in the quickstart's validation record.
7. Remove the drill's callback URL from the App, and destroy the drill (["Tearing
   down"](#the-drill-or-the-load-test)).

## Replacing the host

The data volume, the Elastic IP, and the DNS record outlive the instance, so a new host takes over
the pilot without a restore. Replace it after a change to the bootstrap: the template, or the files
it installs from `deploy/` (`cloudwatch-agent.json` and `systemd/`), after which the plan shows
`aws_instance.host` "must be replaced"; after a host failure that a reboot and automatic recovery do
not fix; or to move to a newer Ubuntu image.

1. Note the running commit: `curl -s https://codeatlas.example.dev/version` (or, on the host,
   `cat /var/lib/codeatlas/state/running`).
2. Plan the replacement and review it: the instance, its volume attachment, and the Elastic IP
   association are replaced; the data volume is not touched. The pilot is down from the moment the
   old instance stops until the release below finishes, so `site-down` and `web-down` fire and then
   recover.

   ```bash
   terraform -chdir=infra/stack init -reconfigure -backend-config=backend.hcl \
     -backend-config=key=stack/pilot.tfstate
   terraform -chdir=infra/stack plan -var-file=pilot.tfvars -replace=aws_instance.host -out=plan.out
   terraform -chdir=infra/stack show plan.out    # review
   terraform -chdir=infra/stack apply plan.out
   ```

3. Wait for the new host's bootstrap (`sudo cloud-init status --wait`). It mounts the same data
   volume, with the database, Caddy's certificates, and `state/running`.
4. The new host has no release bundle or rendered configuration. Release the noted commit to it
   (["Releasing an existing commit"](#releasing-an-existing-commit)); upload its bundle again if it
   expired.
5. Check `/version`, `systemctl list-timers 'codeatlas-*'`, and `systemctl status
   amazon-cloudwatch-agent` (on the host), and that every alarm returns to OK. The alarms on the
   instance's metrics follow the new instance ID in the same apply.

## Losing the data volume

When the pilot's data volume is lost or its file system cannot be repaired, the pilot comes back
from the latest backup, and changes made after that backup are lost (up to 24 hours). If only the
instance failed, replace the host instead (above); the data is intact.

1. Find the latest backup and read its manifest's `created_at` and `release` (["Backups"](#backups)).
2. Make Terraform forget the lost volume. It is protected by `prevent_destroy`, so it is removed from
   the state rather than destroyed:

   ```bash
   terraform -chdir=infra/stack init -reconfigure -backend-config=backend.hcl \
     -backend-config=key=stack/pilot.tfstate
   terraform -chdir=infra/stack state rm 'aws_ebs_volume.pilot_data[0]'
   ```

3. Plan and apply. The plan creates a new data volume and replaces the attachment and the host,
   whose bootstrap names the new volume; check that it destroys nothing else.

   ```bash
   terraform -chdir=infra/stack plan -var-file=pilot.tfvars -out=plan.out
   terraform -chdir=infra/stack show plan.out    # review
   terraform -chdir=infra/stack apply plan.out
   ```

4. Wait for the new host's bootstrap, which creates a file system on the empty volume. Release the
   backup's commit (`release` in its manifest) to the host, as in ["Releasing an existing
   commit"](#releasing-an-existing-commit). It creates an empty database and starts the services;
   Caddy obtains a new certificate.
5. Restore the backup, check the data, and start the services (on the host):

   ```bash
   sudo /opt/codeatlas/current/restore.sh <latest date>
   sudo codeatlas-compose up -d api worker
   ```

6. If `main` has moved past the backup's commit, release the newer commit.
7. Tell the pilot users the backup's time (`created_at`): questions, reviews, and repository changes
   after it are lost, and repositories they disconnected after it are connected again, so they should
   disconnect those again. Lost repository access is found again by the daily access check.
8. Delete the lost volume in the EC2 console (or `aws ec2 delete-volume`) once nothing more can be
   recovered from it; Terraform no longer manages it.

## Fault tests

The fault tests (SC-007) prove that each alert arrives within 10 minutes (the backup alert within an
hour after the 26-hour mark) and that a recovery email follows. They disturb the pilot, so run them
at a quiet time, and record when each fault starts and when each email arrives in the quickstart's
validation record. On the host:

| Fault | Start it | Expected alert | End it |
| --- | --- | --- | --- |
| Edge proxy down | `sudo codeatlas-compose stop caddy` | `site-down` and `web-down` | `sudo codeatlas-compose up -d caddy` |
| Web app down | `sudo codeatlas-compose stop web` | `web-down` | `sudo codeatlas-compose up -d web` |
| Worker down with jobs queued | `sudo codeatlas-compose stop worker`, then re-index a repository in the web app | `worker-down` | `sudo codeatlas-compose up -d worker` |
| Disk full | Fill the data volume past 80%, below | `data-disk-full` | `sudo rm /var/lib/codeatlas/fault-test.fill` |
| Backup skipped | `sudo systemctl disable --now codeatlas-backup.timer`, then wait until 26 hours after the last backup | `backup-missing` | `sudo systemctl enable --now codeatlas-backup.timer`, then `sudo systemctl start codeatlas-backup.service` |

To fill the data volume to 85%:

```bash
FILL="$(df -B1 --output=size,used /var/lib/codeatlas | awk 'NR == 2 { printf "%d", $1 * 0.85 - $2 }')"
sudo fallocate -l "$FILL" /var/lib/codeatlas/fault-test.fill
```

`jobs-waiting`, `jobs-failing`, and `certificate-expiring` are covered by the tests of their
metrics. Prove their notification path by setting their state by hand (on your machine): each sends
an alarm email, then a recovery email. CloudWatch evaluates them again at their next period and
returns them to the state their data gives.

```bash
for alarm in jobs-waiting jobs-failing certificate-expiring; do
  aws cloudwatch set-alarm-state --alarm-name "$alarm" --state-value ALARM \
    --state-reason "Notification test"
done
# After the three alarm emails arrive:
for alarm in jobs-waiting jobs-failing certificate-expiring; do
  aws cloudwatch set-alarm-state --alarm-name "$alarm" --state-value OK \
    --state-reason "Notification test"
done
```

## Running the load test

The load test (SC-011, research R15) measures 001 SC-007's latency targets on the pilot's
hardware; the 10-user level must meet them. It runs against `loadtest`, a temporary environment
built from the pilot's definition: the same instance type, Compose file, and release images, in
fake mode (the fake GitHub gateway and model providers, with the fixture repositories in the
image), with raised daily limits, no rate limit, and no pilot data. The `Load test` workflow
(`.github/workflows/load-test.yml`) runs `evals/perf_check.py` from a GitHub-hosted runner: 10
concurrent users, then 20, 40, and 80, for 120 seconds each, stopping after the first level that
misses a target or returns errors. The environment costs money for as long as it exists, so build,
run, and destroy it in one sitting.

1. Write its parameters: the mode settings (development with fake externals), the raised limits,
   `RATE_LIMIT_PER_MINUTE=0`, and a generated token encryption key and database password. It
   prompts for nothing:

   ```bash
   deploy/put-secrets.sh loadtest
   ```

2. Build the stack. Its instance type must be the pilot's: if `pilot.tfvars` sets
   `instance_type`, set the same value in `loadtest.tfvars`.

   ```bash
   cp infra/stack/loadtest.tfvars.example infra/stack/loadtest.tfvars    # set domain and hostname
   terraform -chdir=infra/stack init -reconfigure -backend-config=backend.hcl \
     -backend-config=key=stack/loadtest.tfstate
   terraform -chdir=infra/stack plan -var-file=loadtest.tfvars -out=plan.out
   terraform -chdir=infra/stack show plan.out    # review
   terraform -chdir=infra/stack apply plan.out
   ```

   Then wait for its bootstrap (`sudo cloud-init status --wait` in a session on the instance tagged
   `codeatlas:environment=loadtest`).
3. Release the pilot's commit to it, following ["Releasing an existing
   commit"](#releasing-an-existing-commit) with:

   ```bash
   SHA="$(curl -s https://codeatlas.example.dev/version | jq -r .commit)"
   ENVIRONMENT=loadtest
   ```

   The release's check confirms that `https://loadtest.example.dev/version` reports the commit.
   The database starts empty; `perf_check` connects the `sample-app` fixture repository and waits
   for its index before the first level.
4. Prepare the readings. EC2 reports CPU every 5 minutes, which spans more than one level, so turn
   on detailed monitoring, which reports it every minute. It is charged while it is on and ends
   with the instance; Terraform does not manage it.

   ```bash
   INSTANCE_ID="$(terraform -chdir=infra/stack output -raw instance_id)"
   aws ec2 monitor-instances --instance-ids "$INSTANCE_ID"

   # The data points of one of the instance's metrics between START and END.
   metric() {    # namespace, metric name, statistic, period in seconds
     aws cloudwatch get-metric-statistics --namespace "$1" --metric-name "$2" \
       --dimensions Name=InstanceId,Value="$INSTANCE_ID" --start-time "$START" --end-time "$END" \
       --statistics "$3" --period "$4" \
       --query "sort_by(Datapoints, &Timestamp)[].[Timestamp, $3]" --output text
   }
   ```

   The instance uses standard CPU credits: once its credit balance is spent, it is held to its
   baseline (20% of each vCPU on a `t4g.medium`), and the levels after that measure a throttled
   host. A new instance may start with a lower balance than the long-running pilot. Check it:

   ```bash
   START="$(( $(date +%s) - 3600 ))" END="$(date +%s)"
   metric AWS/EC2 CPUCreditBalance Minimum 300
   ```

   If the latest value is below 30, wait for it to grow (a `t4g.medium` earns 24 credits an hour;
   one credit is one vCPU at 100% for a minute) and check again.
5. Dispatch the workflow from `main`. `base_url` is the environment's address, the scheme and host
   only (it must equal its `APP_ORIGIN`); keep the default `levels` and `duration` unless there is
   a reason to change them, and state any change in the report:

   ```bash
   gh workflow run load-test.yml --ref main -f base_url=https://loadtest.example.dev \
     -f levels=10,20,40,80 -f duration=120
   sleep 5
   RUN_ID="$(gh run list --workflow load-test.yml --limit 1 \
     --json databaseId --jq '.[0].databaseId')"
   gh run watch "$RUN_ID"
   ```

   The run's Summary page shows the network baseline (the median connection and TLS times of 20
   requests to `/healthz`) and the table. A level that misses a target ends the run there with a
   notice, and the job still succeeds; it fails only when `perf_check` cannot set up (exit status
   2), for example because `base_url` is not the environment's address or the release is not
   running. The step's log names the cause.
6. Download the artifact (`backend/evals/out/` is ignored by Git):

   ```bash
   gh run download "$RUN_ID" --name load-test --dir backend/evals/out/load-test
   ```

   It holds `level-<users>.json` for each level that ran (the `perf_check --json` summary, with
   the commit from `/version`), `network.txt` and `network.md` with the baseline, and
   `load-test.md` with the baseline and the table from `evals/load_report.py`.
7. Read the peak CPU and memory of each level. The log has each level's start; a level ends when
   the next one starts, or when the step ends:

   ```bash
   gh run view "$RUN_ID" --log | grep -E 'Level: [0-9]+ users'    # each level's start time
   gh run view "$RUN_ID" --json startedAt,updatedAt              # the run's start and end
   START=<the run's start, for example 2026-10-09T14:00:00Z>
   END=<the run's end>
   metric AWS/EC2 CPUUtilization Maximum 60       # percent of both vCPUs, per minute
   metric CWAgent mem_used_percent Maximum 60     # percent of the host's memory, per minute
   metric AWS/EC2 CPUCreditBalance Minimum 300    # whether the balance reached zero
   ```

   A level's peak is the highest value among the minutes within it.
8. Destroy the stack and delete its parameters (["The drill or the load
   test"](#the-drill-or-the-load-test)).
9. Write `docs/reports/load-test.md` from the artifact's `load-test.md` and the readings: the steps
   and inputs to rerun it (the workflow inputs, the variable file's settings with `example.dev` as
   the domain, and the commit); the hardware (instance type, vCPUs, memory, and volumes); the
   configuration (fake mode, the raised limits, no rate limit); the duration; the network baseline,
   noting that the runner's region is not guaranteed; each level's p95 latency and error rate by
   category; the highest level that meets every target, or "at least 80" when every level does;
   each level's peak CPU and memory; and the credit balance at the start and its minimum. If peak
   memory stays under half of the host, research R1 calls for trying a `t4g.small`; record the
   result in ADR 0010.

## Tearing down

### The drill or the load test

Destroy the stack with its own state key and variable file. Its data volume is not protected:

```bash
terraform -chdir=infra/stack init -reconfigure -backend-config=backend.hcl \
  -backend-config=key=stack/loadtest.tfstate
terraform -chdir=infra/stack plan -destroy -var-file=loadtest.tfvars -out=plan.out
terraform -chdir=infra/stack show plan.out    # review
terraform -chdir=infra/stack apply plan.out
```

For the load test, also delete its parameters:

```bash
aws ssm get-parameters-by-path --path /codeatlas/loadtest --recursive \
  --query 'Parameters[].Name' --output text | tr '\t' '\n' | xargs -n 10 aws ssm delete-parameters --names
```

### The pilot

Destroying the pilot deletes its database. Its data volume has `prevent_destroy`, so the destroy
plan fails until you remove that protection deliberately:

1. Tell the pilot users, and remove the App's installations or ask them to.
2. In your working copy only, delete the `prevent_destroy = true` line of `aws_ebs_volume.pilot_data`
   in `infra/stack/main.tf`.
3. Initialize with `key=stack/pilot.tfstate`, then plan with `-destroy -var-file=pilot.tfvars`,
   check that the plan lists the data volume, and apply it.
4. Restore the line with `git checkout infra/stack/main.tf`.
5. Delete the pilot's parameters, as for the load test with `/codeatlas/pilot`, and delete the
   pilot GitHub App (its settings, Advanced, Delete GitHub App).

### Shared resources

After every stack is gone, empty the image repositories and the bucket (Terraform refuses to delete
them otherwise), then destroy `infra/shared`:

```bash
for repository in codeatlas/api codeatlas/web; do
  ids="$(aws ecr list-images --repository-name "$repository" --query 'imageIds[*]' --output json)"
  [ "$ids" = "[]" ] || aws ecr batch-delete-image --repository-name "$repository" --image-ids "$ids"
done
aws s3 rm "s3://$BUCKET" --recursive

terraform -chdir=infra/shared plan -destroy -var-file=shared.tfvars -out=plan.out
terraform -chdir=infra/shared show plan.out    # review
terraform -chdir=infra/shared apply plan.out
```

Finally, remove the delegation at the registrar, and empty (all versions) and delete the state
bucket in the S3 console.
