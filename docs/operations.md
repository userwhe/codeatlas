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

`infra/shared/` holds the image repositories, the bucket for release bundles, and the hosted zone.
Copy the examples and fill them in: the account ID in `backend.hcl`; the domain, the alert email
address, and the repository's OIDC subject prefix in `shared.tfvars` (later stories use the last
two, but the variables are required now).

```bash
cp infra/shared/backend.hcl.example infra/shared/backend.hcl
cp infra/shared/shared.tfvars.example infra/shared/shared.tfvars
gh api repos/<owner>/<repo>/actions/oidc/customization/sub   # the OIDC subject prefix

terraform -chdir=infra/shared init -backend-config=backend.hcl
terraform -chdir=infra/shared plan -var-file=shared.tfvars -out=plan.out
terraform -chdir=infra/shared show plan.out    # review
terraform -chdir=infra/shared apply plan.out
```

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

The host installs Docker and the AWS CLI and mounts its data volume on first boot. Wait for it to
finish (on the host):

```bash
sudo cloud-init status --wait    # "status: done"
```

Then release the current `main` commit by hand (next section) and add the pilot users (["Managing
pilot users"](#managing-pilot-users)), starting with yourself.

## Releasing by hand

A release puts one commit on a host: both images tagged with the full commit SHA in ECR, the
commit's `deploy/` directory as a bundle in S3, and a Run Command that runs the bundle's
`release.sh`. The script renders the configuration, pulls the images, migrates, switches to the
new release, and waits until `/readyz` answers through Caddy.

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

`release.sh` exits with:

- **0**: released; `/version` reports the commit and the footer shows its first seven characters.
- **2**: it failed before the switch (configuration, registry login, pull, or migration). The
  previous release is still serving, untouched; the output names the failing step, for example a
  missing parameter.
- **1**: it started the new release, but `/readyz` did not answer within 120 seconds. Look at the
  services (on the host: `sudo codeatlas-compose ps` and `sudo codeatlas-compose logs api`), then
  fix forward or roll back.

To roll back by hand, release the previous commit. Its bundle is still on the host (on the host):

```bash
PREVIOUS="$(cat /var/lib/codeatlas/state/previous)"
sudo /opt/codeatlas/releases/$PREVIOUS/release.sh "$PREVIOUS"
```

`/opt/codeatlas/releases/` keeps every bundle released on the host; remove old ones by hand when
the disk needs it, never the ones named in `state/running` and `state/previous`.

### Releasing an existing commit

The drill, the load test, and a replaced host use the same steps for a commit that was released
before: set `ENVIRONMENT` to the host's environment, skip the image builds (the tags exist), upload
the bundle again only if it expired, and send the Run Command to that host. A replaced pilot host
takes the commit recorded on its data volume, in `/var/lib/codeatlas/state/running`; `/version`
shows it too while the old host still answers.

## Temporary settings

The optional limits and switches (`DAILY_QUESTION_LIMIT`, `DAILY_REVIEW_LIMIT`,
`PILOT_DAILY_QUESTION_LIMIT`, `PILOT_DAILY_REVIEW_LIMIT`, `RATE_LIMIT_PER_MINUTE`, and
`EMIT_METRICS`) change through parameters and take effect at the next release:

```bash
deploy/put-secrets.sh pilot --set PILOT_DAILY_QUESTION_LIMIT=2
```

Then release the running commit again. Its bundle is on the host, so this is enough (on the host):

```bash
RUNNING="$(cat /var/lib/codeatlas/state/running)"
sudo /opt/codeatlas/releases/$RUNNING/release.sh "$RUNNING"
```

The release renders the new configuration and recreates the services whose configuration
changed. To return to the default, remove the setting and release again:

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

Replace the parameter, then release the running commit again (see ["Temporary
settings"](#temporary-settings)): the release renders the new value and recreates `api` and
`worker`.

```bash
deploy/put-secrets.sh pilot --overwrite <NAME>
```

| Credential | Steps |
| --- | --- |
| `GEMINI_API_KEY`, `VOYAGE_API_KEY` | Create a new key in the provider's console, overwrite the parameter, release, then revoke the old key |
| `GITHUB_APP_CLIENT_SECRET` | Generate a new client secret in the App's settings, overwrite, release, then delete the old secret |
| `GITHUB_APP_PRIVATE_KEY` | Generate a new private key in the App's settings, run `put-secrets.sh pilot --overwrite GITHUB_APP_PRIVATE_KEY --github-app-private-key <file>`, release, then delete the old key in the App's settings and the downloaded file |
| `GITHUB_WEBHOOK_SECRET` | Generate one with `openssl rand -hex 32`, overwrite, release, and set it in the App's webhook settings right away. Deliveries in between fail their signature check; redeliver them from the App's Recent Deliveries page, or let the daily check find the missed pushes |
| `TOKEN_ENCRYPTION_KEY` | `put-secrets.sh pilot --overwrite TOKEN_ENCRYPTION_KEY` generates a new key; release. The stored GitHub tokens become unreadable, so every user is asked to sign in again; their repositories stay connected |

### Replacing the database password

PostgreSQL reads `POSTGRES_PASSWORD` only when it creates its data directory, so the password
changes in the database first, then in the parameter, then in the running services. `put-secrets.sh`
refuses to overwrite it for that reason. Do the three steps back to back: until the release, new
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

4. Release the running commit again. It writes the new `DATABASE_URL` and recreates `api`, `worker`,
   and `db`.

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
