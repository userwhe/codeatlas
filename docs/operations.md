# Operations guide

How to build, release, and run the CodeAtlas pilot on Railway. The project `codeatlas` runs the
pilot in its `production` environment: the services `web`, `api`, `worker`, `backup`, and `db`, as
recorded in [`.railway/railway.ts`](../.railway/railway.ts). A separate project,
`codeatlas-backups`, holds the bucket `backups` with the nightly backups, so that losing the
pilot's environment or project does not lose them. The interfaces (environments, services,
variables, operator commands, the release flow, the monitors, logs, and metrics) are in
[specs/004-pilot-deployment/contracts/operations.md](../specs/004-pilot-deployment/contracts/operations.md),
and the decisions behind them in [research.md](../specs/004-pilot-deployment/research.md) and ADRs
0013 to 0016.

## Conventions

- **Developer actions**: every step that writes to an external system is a developer action:
  `railway config apply`, projects and buckets, variables and secrets, environments, domains,
  deployments, removals and rollbacks, the GitHub App, notification and usage settings, the
  monitors, merges into `main` (each of which releases), workflow dispatches, and operator commands
  on the pilot. An agent may prepare the exact commands and summaries, and runs nothing until the
  developer confirms that draft; any change to the draft needs a new confirmation. Reading needs no
  confirmation: `railway config plan`, `railway status`, `railway deployment list`,
  `railway logs`, `railway metrics`, and requests to the public paths.
- Commands run from the repository root, on your machine, signed in with `railway login`. Link the
  directory to the pilot once:

  ```bash
  railway link --project codeatlas --environment production
  ```

  `-s <service>` selects a service, and `-e <environment>` another environment. `railway config`
  and `railway redeploy` act on the linked environment; for `drill` or `loadtest`, switch with
  `railway environment <name>` first, and back with `railway environment production` afterward.
- The bucket's commands act on the project `codeatlas-backups`. Run them from a directory of their
  own outside the repository, linked to that project (["The backups bucket"](#the-backups-bucket)),
  so that the repository stays linked to the pilot. This guide runs them in a subshell, for
  example `(cd ~/codeatlas-backups && railway bucket credentials -b backups)`, which prints the
  bucket's keys; the bucket's Credentials tab in the dashboard shows the same values.
- `<domain>` is the web app's domain, chosen in ["The domain"](#the-domain): the pilot's is
  `codeatlas-pilot.up.railway.app`.
- **Operator commands** run in a service's container, with that service's variables, through the
  platform's SSH (an SSH key registered on your Railway account, and the host key of
  `ssh.railway.com` accepted; ["Tools and accounts"](#tools-and-accounts)):

  ```bash
  railway ssh -s api -- python -m codeatlas.ops <command>
  ```

  The one-line form passes the command through SSH, which may not keep its quoting, so run a
  command whose arguments contain spaces or quotes in an interactive session: `railway ssh -s api`
  opens a shell in the container, and `exit` ends it.
- **Secrets** are sealed variables. Railway gives a sealed value to the service's deployments, never
  shows it again, and does not copy it into a duplicated environment. Only the dashboard seals a
  variable (the variable's menu, "Seal"; a seal cannot be undone), and a sealed variable is changed
  from the same menu ("Edit"), where it stays sealed. Set a new secret from standard input, so that
  it stays off the command line and out of the shell history, then seal it at once on every
  service that has it:

  ```bash
  read -rs VALUE    # paste the value and press Enter; nothing is shown
  for service in api worker; do
    printf '%s' "$VALUE" | railway variable set <NAME> --stdin -s "$service" --skip-deploys
  done
  unset VALUE
  ```

  Or add it in the dashboard (the service's Variables, New Variable) and seal it there. Keep a copy
  of every secret in your password manager: a sealed value cannot be read back, and a copied or
  rebuilt environment needs it again.
- **Staged changes**: a variable or setting changed with `--skip-deploys` or in the dashboard is
  staged. The dashboard lists the environment's staged changes, each with its old and new value
  (secrets hidden), and "Deploy" applies them and redeploys the services they touch. Networking
  changes, such as domains, apply at once.

## Environments

| Environment | Made by | Lifetime | Mode | Monitors and alerts |
| --- | --- | --- | --- | --- |
| `production` (the pilot) | `railway config apply` from `.railway/railway.ts` | Always on | `CODEATLAS_ENV=production` | Yes |
| `drill` | `railway environment new drill --duplicate production` | A recovery exercise or a failed-release rehearsal | Production, with its own domain and secrets set again; no worker deployment | No |
| `loadtest` | `railway environment new loadtest --duplicate production` | A load test | Development with fake externals; no backup schedule | No |

A duplicated environment copies the services, their configuration, and the non-secret variables.
It gets empty volumes and its own generated domain, and does not receive sealed variables, so its
`backup` service has no bucket keys, and a nightly run there fails without writing anything. Its
services are staged until you deploy them. It deploys from `main` like the pilot, so while it
exists, a merge releases there too. Delete it after use (["Tearing down"](#tearing-down)).
Railway's notification rules cover every environment of the project, so a temporary
environment's failed deployments email you as well; the monitors watch only the pilot.

## One-time setup

### Tools and accounts

- A Railway account on the Hobby plan, connected to GitHub, with access for Railway's GitHub App to
  this repository before the first `railway config apply` (on GitHub: Settings, Applications,
  Railway, Configure, and add the repository). Without it, `apply` creates the app services with
  no deployment trigger (["Wait for CI"](#wait-for-ci)). "Wait for CI" needs the App's updated
  permissions; accept them on GitHub when Railway asks.
- The Railway CLI, a version with `railway config` and `railway ssh` (`railway upgrade` updates
  it), signed in with `railway login`. The pilot was built with 5.66.0.
- An SSH key registered on the Railway account, for `railway ssh`:

  ```bash
  railway ssh keys add --key ~/.ssh/id_ed25519.pub --name <this machine>
  railway ssh keys list
  ```

  `railway ssh` checks the host key of `ssh.railway.com`. In a shell that is not interactive, such
  as a script, it fails with "Host key verification failed" until that key is in
  `~/.ssh/known_hosts`. Connect once interactively and accept the key, or add it after checking
  its fingerprint:

  ```bash
  ssh-keyscan -t ed25519 ssh.railway.com | ssh-keygen -lf -    # check the fingerprint
  ssh-keyscan -t ed25519 ssh.railway.com >> ~/.ssh/known_hosts
  ```

  When the registered key is not one SSH finds by itself, choose it with
  `railway ssh -i <private key file> -s <service>`.
- Node.js 24 with npm, and the Railway CLI 5.42.1 or later: `railway config` runs
  `.railway/railway.ts` with Railway's SDK, the npm package `railway`, pinned in
  `.railway/package.json`. Install it before the first plan, and again after the pin changes:

  ```bash
  npm ci --prefix .railway
  ```

- `openssl`, and `uv` for `backend/`, to generate secrets; the GitHub CLI, `gh`, for the load test,
  and with `jq` for the App's manifest route.
- Gemini (paid tier) and Voyage AI keys, as in 001.
- A Better Stack account on the free plan, for the monitors (["Alerts"](#alerts)).
- A password manager for the secrets.

### The project

Grant Railway's GitHub App access to the repository first (["Tools and
accounts"](#tools-and-accounts)). Then create the project and link this directory to its
`production` environment, preview the configuration, and apply it:

```bash
railway init --name codeatlas
railway link --project codeatlas --environment production
railway config plan
railway config apply
```

In a shell that is not interactive, `railway init` also needs `--workspace <workspace ID>`;
`railway whoami --json` lists the workspaces with their IDs. The plan lists every service, the
volume, and every variable, with secret values redacted; the secrets declared with `preserve()`
get their values below. `apply` runs the plan again, shows it, and asks for confirmation. It is
rejected if the environment changed in between; plan again then.

### The backups bucket

The nightly backups go to a bucket in a project of their own, so that they outlive the pilot's
environment and project. Create the project and its bucket once, in US East (`iad`), from a
directory outside the repository; `railway init` links that directory to the new project:

```bash
mkdir -p ~/codeatlas-backups
cd ~/codeatlas-backups
railway init --name codeatlas-backups
railway bucket create backups --region iad
cd -
```

The bucket is private and encrypted at rest. It has no lifecycle rules: the backup command prunes
old backups itself. Its five values reach `backup` in ["Variables and
secrets"](#variables-and-secrets), before the first nightly run.

### Wait for CI

From its first deployment, each app service waits for CI. `apply` turns it on with each service's
source (`checkSuites` in the file). Check `web`, `api`, `worker`, and `backup` (each service's
Settings, Source): "Wait for CI" is on, and the branch is `main`.

If Railway's GitHub App could not see the repository when `apply` ran, the app services have no
deployment trigger: every deployment fails before building, with "Deployment does not have an
associated build", and "Wait for CI" does not stick. Grant the access (["Tools and
accounts"](#tools-and-accounts)), then create each app service's trigger with "Wait for CI". In the
dashboard, reconnect the repository in the service's Settings, Source, which does both. Or use the
CLI's GraphQL access, once per service, with the root directory `frontend` for `web` and `backend`
for the others:

```bash
railway api 'mutation($input: DeploymentTriggerCreateInput!) {
  deploymentTriggerCreate(input: $input) { id checkSuites validCheckSuites }
}' --variables '{"input": {"projectId": "<project ID>", "environmentId": "<environment ID>",
  "serviceId": "<service ID>", "provider": "github", "repository": "userwhe/codeatlas",
  "branch": "main", "rootDirectory": "backend", "checkSuites": true}}'
```

`railway status --json` has the project's, the environment's, and the services' IDs.
`validCheckSuites: 1` in the answer confirms that Railway sees the CI workflow.

If a deployment started before "Wait for CI" was on, abort it (the service's Deployments, the
deployment's menu, "Abort deployment"). Pull request environments stay off in the project's
settings. CI runs on every push to `main`, which "Wait for CI" requires.

### The domain

Generate the web app's domain on port 3000, then rename it to the name the pilot keeps. The first
command prints the generated name, and the second its ID. `--domain` takes the full domain:

```bash
railway domain -s web --port 3000
railway domain list -s web --json
railway domain update <domain ID> -s web --domain codeatlas-pilot.up.railway.app
railway domain list -s web
```

`codeatlas` was taken, so the pilot's address is `https://codeatlas-pilot.up.railway.app`. If you
rebuild and that name is taken too, choose the nearest free name, and record it in ["Settings
outside the infrastructure file"](#settings-outside-the-infrastructure-file). Railway's edge serves
the domain over HTTPS with a certificate it renews, and answers HTTP with a 301 to HTTPS; every
page carries `Strict-Transport-Security`. Only `web` has a domain: `api`, `worker`, `backup`, and
`db` have none, and `db` has no TCP proxy.

### The pilot GitHub App

The pilot uses its own App, separate from the development App: `CodeAtlas Pilot`, with the slug
`codeatlas-pilot`. Register it with GitHub's form, or from a manifest (below). In GitHub, open
Settings, then Developer settings, then GitHub Apps, then New GitHub App, and set:

- **GitHub App name**: `CodeAtlas Pilot`; **Homepage URL**: `https://<domain>`.
- **Callback URL**: `https://<domain>/auth/github/callback`.
- **Expire user authorization tokens**: on.
- **Webhook**: active, with the URL `https://<domain>/webhooks/github` and a secret generated with
  `openssl rand -hex 32`. Keep the secret in your password manager for the next step.
- **Repository permissions**: Contents, Metadata, and Pull requests, each `Read-only`. No other
  permissions.
- **Events**: subscribe to Push. Installation and Installation repositories events reach every App
  without a subscription.
- **Where can this GitHub App be installed**: any account, so pilot users can install it.

After creating it, note the App ID, the client ID, and the slug (the last part of the App's public
page URL), generate a client secret, and generate a private key, which downloads a `.pem` file.

**From a manifest**, instead of the form: GitHub creates the App with these settings and returns
its values, secrets included, so that they go to Railway without being shown. Save a page that
posts the manifest to GitHub, open it in your browser, and submit it:

```html
<form action="https://github.com/settings/apps/new" method="post">
  <input type="hidden" name="manifest" value='{
    "name": "CodeAtlas Pilot",
    "url": "https://<domain>",
    "hook_attributes": {"url": "https://<domain>/webhooks/github"},
    "redirect_url": "<a page to return to>",
    "callback_urls": ["https://<domain>/auth/github/callback"],
    "public": true,
    "default_permissions": {"contents": "read", "metadata": "read", "pull_requests": "read"},
    "default_events": ["push"]
  }'>
  <button type="submit">Create the pilot App</button>
</form>
```

GitHub shows the App; choose Create. It then sends the browser to `redirect_url` with a one-time
`code` in the query string. Exchange the code once, and set the six values on `api` and `worker`
straight from the answer:

```bash
APP="$(gh api --method POST /app-manifests/<code>/conversions)"
for service in api worker; do
  for pair in GITHUB_APP_ID=.id GITHUB_APP_SLUG=.slug GITHUB_APP_CLIENT_ID=.client_id \
    GITHUB_APP_CLIENT_SECRET=.client_secret GITHUB_WEBHOOK_SECRET=.webhook_secret \
    GITHUB_APP_PRIVATE_KEY=.pem; do
    printf '%s' "$APP" | jq -j "${pair#*=}" |
      railway variable set "${pair%%=*}" --stdin -s "$service" --skip-deploys
  done
done
```

Copy the three secrets into your password manager without printing them, for example
`printf '%s' "$APP" | jq -j .pem | pbcopy` for the private key, then `unset APP`. In the App's
settings, check that "Expire user authorization tokens" is on. The App's secrets are sealed with
the others in the next step.

### Variables and secrets

After the manifest route, the App's six values are set already; set the other secrets below, and
seal them all. With the form, the App's ID, slug, and client ID are not secrets. Set them on `api`
and `worker`, unsealed:

```bash
for service in api worker; do
  railway variable set GITHUB_APP_ID=<App ID> GITHUB_APP_SLUG=<slug> \
    GITHUB_APP_CLIENT_ID=<client ID> -s "$service" --skip-deploys
done
```

Set each secret on the services that use it, as in ["Conventions"](#conventions), and seal it:

| Secret | Services | Value |
| --- | --- | --- |
| `GITHUB_APP_CLIENT_SECRET` | `api`, `worker` | The App's client secret |
| `GITHUB_APP_PRIVATE_KEY` | `api`, `worker` | The content of the downloaded `.pem` file, below |
| `GITHUB_WEBHOOK_SECRET` | `api`, `worker` | The App's webhook secret |
| `TOKEN_ENCRYPTION_KEY` | `api`, `worker` | A new key: `(cd backend && uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")` |
| `GEMINI_API_KEY`, `VOYAGE_API_KEY` | `api`, `worker` | The providers' keys |
| `POSTGRES_PASSWORD` | `db` | Generated once, below |
| `BACKUP_ENDPOINT_URL`, `BACKUP_REGION`, `BACKUP_BUCKET`, `BACKUP_ACCESS_KEY_ID`, `BACKUP_SECRET_ACCESS_KEY` | `backup` | The bucket's values, below |

The private key spans several lines, so set it from the file, then delete the file or move it to
offline storage:

```bash
for service in api worker; do
  railway variable set GITHUB_APP_PRIVATE_KEY --stdin -s "$service" --skip-deploys \
    < <path to the downloaded .pem file>
done
```

The database password must hold only letters and digits, because the reference variable puts it
into `DATABASE_URL`. Generate it straight into the variable; only the reference reads it:

```bash
openssl rand -hex 24 | tr -d '\n' |
  railway variable set POSTGRES_PASSWORD --stdin -s db --skip-deploys
```

Copy the bucket's five values to `backup`. Print them, then paste each one as its name appears:

```bash
(cd ~/codeatlas-backups && railway bucket credentials -b backups)
for name in BACKUP_ENDPOINT_URL BACKUP_REGION BACKUP_BUCKET BACKUP_ACCESS_KEY_ID \
  BACKUP_SECRET_ACCESS_KEY; do
  printf '%s: ' "$name"; read -rs VALUE; echo
  printf '%s' "$VALUE" | railway variable set "$name" --stdin -s backup --skip-deploys
done
unset VALUE
```

| Variable on `backup` | Printed by `railway bucket credentials` as | Field with `--json` |
| --- | --- | --- |
| `BACKUP_ENDPOINT_URL` | `AWS_ENDPOINT_URL` | `endpoint` |
| `BACKUP_REGION` | `AWS_DEFAULT_REGION` | `region` (`auto`) |
| `BACKUP_BUCKET` | `AWS_S3_BUCKET_NAME` | `bucketName` |
| `BACKUP_ACCESS_KEY_ID` | `AWS_ACCESS_KEY_ID` | `accessKeyId` |
| `BACKUP_SECRET_ACCESS_KEY` | `AWS_SECRET_ACCESS_KEY` | `secretAccessKey` |

`--json` also reports `urlStyle`, `virtual-host`: the virtual-hosted addressing the backup command
uses. Then seal every secret in the dashboard, the bucket's five values included. `api` and
`worker` hold the same secrets; `web` holds none, and `backup` holds only the bucket's values,
which the bucket can print again at any time.

### The first deployment

Review the staged changes in the dashboard. Variables set with `--skip-deploys` take effect at each
service's next deployment. Deploy the services in this order; each app service's deployment runs as
in ["Releases"](#releases):

1. `db`, once `POSTGRES_PASSWORD` is set. Without it, the pgvector image refuses to initialize the
   database, so a `db` deployment that started before the password was set failed. Deploy it
   again with `railway redeploy -s db`, and wait until it runs (`railway logs -s db --lines 20`).
2. `api`: its pre-deploy command migrates the empty database (0001 to 0005 at the pilot's first
   deployment), and it takes traffic once `/readyz` answers.
3. `web`, `worker`, and `backup`: their release gates pass once `api` reports their commit.

Deploy an app service at the current `main` commit from its Command Palette (Cmd+K), "Deploy
Latest Commit", or with the CLI's GraphQL access (the IDs as in ["Wait for CI"](#wait-for-ci)):

```bash
railway api 'mutation { serviceInstanceDeploy(serviceId: "<service ID>",
  environmentId: "<environment ID>", latestCommit: true) }'
```

Watch each deployment, then check from outside:

```bash
railway deployment list -s api
railway logs -s worker --latest
curl -s https://<domain>/version    # {"commit": "<the main commit>"}
curl -sI http://<domain>/           # 301 to https://<domain>/
```

The access list is empty, so add yourself first, then the other pilot users (["Managing pilot
users"](#managing-pilot-users)). Then set up the alerts (["Alerts"](#alerts)), and record every
setting you changed in the dashboard in the table below.

## Settings outside the infrastructure file

`.railway/railway.ts` cannot hold these settings, so they are recorded here with their values.
Change them in the dashboard (["Changing the configuration"](#changing-the-configuration)), and
update this table with every change.

The first `railway config plan` confirmed that the file applies the size limits, the cron
schedule, "Wait for CI", and the pre-deploy timeout, so they are not in this table. "Wait for CI"
takes effect only when Railway's GitHub App can see the repository at `apply` (["Wait for
CI"](#wait-for-ci)).

| Setting | Where | Value |
| --- | --- | --- |
| The web app's generated domain | `web`, Settings, Networking (or `railway domain`) | `codeatlas-pilot.up.railway.app` (`codeatlas` was taken) |
| Notification rules | Account, Notifications, with a project override | Email for build failed, deployment failed, and deployment crashed |
| Usage limits | Workspace, Usage | Custom email alert $20; hard limit $50 |
| SSH key | Account, SSH keys | The developer's public key |
| The backups project and its bucket | Project `codeatlas-backups` (`railway init`, `railway bucket create`) | Bucket `backups` in US East (`iad`); its five values copied to `backup` in `production` and sealed |
| Pull request environments | Project, Settings, Environments | Off (the default) |

The pilot GitHub App ([above](#the-pilot-github-app)) and the Better Stack monitors
(["Monitors"](#monitors)) are also outside the file; their settings are in those sections.

Two settings stay at Railway's defaults, so the file leaves them unset. Railway builds each app
service with the Dockerfile in its root directory by itself (the build log shows `load build
definition from backend/Dockerfile`); its API has no Dockerfile builder value, and the dashboard
may still display Railpack. The restart policy of every service except `backup` is Railway's
default, on failure with 10 retries, which the platform does not report back.

## Releases

Every commit pushed to `main` is released once CI passes, with no approval step: the merge of a
reviewed pull request is your confirmation. A direct push to `main` releases too, so merge through
pull requests only. Railway deploys `web`, `api`, `worker`, and `backup` from the commit, each on
its own; releases do not redeploy `db`. For each of these services:

1. **Wait for CI**: the deployment waits (`WAITING`) until every GitHub Actions workflow on the
   commit has finished. If one fails, Railway skips the deployment; if they have not all finished
   after 2 hours, it skips it too.
2. **Build** from the service's Dockerfile, with its root directory as the context. Hobby builds at
   most three services at a time, so the fourth waits its turn.
3. **Pre-deploy command**, in its own container with the service's variables:
   - `api`: `python -m codeatlas.ops migrate`. Under a PostgreSQL advisory lock, it upgrades the
     schema when the database's revision is one this release knows (or there is none). When the
     database is ahead, after a rollback, it logs `migrations_skipped` with both revisions and
     exits 0. A failed migration exits 1.
   - `web`, `worker`, and `backup`: the release gate (`node scripts/wait-for-api.mjs` in the web
     image, `python -m codeatlas.ops wait-for-api` in the backend image). It polls
     `API_ORIGIN/version` over the private network every 5 seconds until `api` reports the
     deployment's own commit, and fails after 20 minutes (the pre-deploy timeout is 1,500
     seconds). `api` reports a commit only after its migration succeeded and its readiness check
     passed.
4. **Start**, and for `web` and `api`, the **healthcheck**: traffic moves to the new deployment
   only after `/` (`web`) or `/readyz` (`api`) returns 2xx within 300 seconds.
5. **Old deployment**: `web` and `api` overlap for 10 seconds, then the old one gets `SIGTERM` and
   10 seconds before `SIGKILL`; the old `worker` gets `SIGTERM` and 30 seconds. The worker stops
   after its current job; a job still running at `SIGKILL` loses its lease within 60 seconds, and
   the new worker resumes it as a new attempt.

A failed build, pre-deploy command, or healthcheck marks that deployment failed, the previous one
keeps serving, and Railway emails you. When `api` fails, in its migration or its readiness check,
the other services' gates time out after 20 minutes and their deployments fail too, so the whole
release stops and every service keeps its previous version. Nothing needs undoing: fix the cause
through a pull request, whose merge releases again.

Between `api`'s switch and the gated services' switches, for a minute or so, the API may run a
newer commit than the web app and the worker. Changes that need both together follow the
expand-then-contract rule below.

When two merges land within minutes, the older commit's gated deployments may wait for a commit
that `api` never reports, and fail after 20 minutes, which emails you; the newer commit's
deployments proceed. Expect those failures, and check that the newer commit serves.

Watch a release in the dashboard (each service's Deployments), or:

```bash
railway deployment list -s api                  # each deployment and its status
railway logs -s api --latest                    # the newest deployment, even building or failed
railway logs -s api --build --latest            # its build
railway logs -s worker --latest --filter "release_gate_waiting"
curl -s https://<domain>/version                # the commit api reports
```

A release serves within about 15 minutes of the merge: about 5 for CI, then the builds. Every
page's footer shows the first seven characters of the running commit.

## Migrations: expand, then contract

A release migrates the database before its new version starts, a rollback restores earlier images
but not the schema, and for a minute the previous version runs against the new schema. Every
migration must therefore keep the previous release working:

- **Expand** in one release: add tables, nullable columns or columns with a server default, and
  indexes. The previous release ignores what it does not know.
- **Contract** in a later release, once neither the running release nor the one you might roll
  back to uses the old shape: drop the old column or table, or tighten a constraint.
- Never drop, rename, or change the type of a column in the release that stops using it. A rename
  takes three releases: add the new column and write both (with a backfill); read only the new
  one; drop the old one. A change that would break the running version is split across pull
  requests, released one after the other.

CI's `schema` job runs on every pull request. It lists every file the pull request adds, modifies,
deletes, renames, or copies under `backend/alembic/versions/`, with its status, in the job's
summary with this rule, and raises a "Schema change" warning annotation; with none, it writes "No
schema change". It never fails. Check every listed migration against these rules before merging.

Variables follow the same rule: add a variable before merging the code that needs it, and remove
it only after the code stops reading it.

## Rolling back

Roll back a release that passed its checks but misbehaves. A release is rolled back as a whole,
`api` first, then `worker`, `web`, and `backup`. Hobby keeps a replaced deployment's image for 72
hours; within that window, "Rollback" redeploys the image and its variables without a rebuild:

1. In `api`'s Deployments, open the previous deployment's menu, choose "Rollback", and confirm.
2. Do the same for `worker`, `web`, and `backup`.
3. Check that `curl -s https://<domain>/version` reports the earlier commit. Each service serves
   again within 5 minutes.

The CLI has no rollback command. Three things to know:

- **Schema**: migrations already applied stay. If Railway runs the rolled-back `api`'s pre-deploy
  command, `migrate` finds the database ahead, logs `migrations_skipped`, and the API starts
  against the newer schema, which the expand-then-contract rule keeps working; the other services'
  gates then pass, because `api` reports the earlier commit. If Railway does not run pre-deploy
  commands on a rollback, the order does no harm. The quickstart's validation record notes which
  applies (SC-010).
- **Variables**: a rollback restores the deployment's variables too. If you changed one since, such
  as a replaced credential, set it again afterward.
- **Going forward**: the next merge to `main` releases as usual. Fix or revert the bad change
  through a pull request.

After the 72 hours, redeploy the earlier commit instead. It builds again and serves within about 15
minutes:

- revert the change on `main` through a pull request (`git revert <commit>` on a branch), which
  releases normally; or
- in each service's Deployments, `api` first, open the earlier deployment's menu and choose
  "Redeploy", which builds the same commit with the same configuration. `railway redeploy`
  redeploys only a service's latest deployment, so it cannot choose an earlier commit.

## Rehearsing a failed release

The rehearsals show that a failed readiness check keeps the previous version serving (SC-010), and
that a failure of `api` stops the whole release. During each, run the probe from the quickstart
(scenario 12), which reports every failed request to `/readyz` and `/`:

```bash
while :; do for p in readyz ''; do
  curl -s -o /dev/null -w "/$p %{http_code}\n" "https://<domain>/$p"
done; sleep 1; done
```

**A failed readiness check**, on the pilot (quickstart scenario 15):

1. In `api`'s settings, change the healthcheck path to `/readyz-missing`, and deploy the staged
   change. Note the time.
2. The new deployment fails its healthcheck after 300 seconds and never takes traffic; the previous
   one serves throughout, and Railway emails a failed deployment.
3. Restore the path `/readyz`, and deploy again. `railway config plan` then shows no difference.
4. Rehearse a rollback (["Rolling back"](#rolling-back)). Record the times, and whether Railway ran
   the gates on the rollback, in the quickstart's validation record.

**The whole release stops**, in `drill` (quickstart scenario 22):

1. Build `drill` up to its first deployment: steps 1 to 3 of ["Recovery
   exercise"](#recovery-exercise). No restore is needed.
2. In `drill`, change `api`'s healthcheck path to `/readyz-missing`, and deploy the staged change.
3. Merge a visible change to `main` through a pull request.
4. In `drill`, `api`'s new deployment fails its healthcheck, the gates of `web`, `worker`, and
   `backup` time out after 20 minutes, and their deployments fail; every service there keeps the
   previous commit (`curl -s https://<drill domain>/version`). The pilot releases the change
   normally.
5. Delete `drill` (["Tearing down"](#tearing-down)).

A failed migration stops a release through the same gate, because `api` then never reports the new
commit; the integration tests cover the migration command's own failure.

## Changing the configuration

- **Settings the file holds** (services, sources with "Wait for CI", commands, healthchecks,
  `backup`'s restart policy and cron schedule, the pre-deploy timeout, the size limits, the volume,
  and the variables): change `.railway/railway.ts` through a pull request.
  After it merges, from an up-to-date `main`:

  ```bash
  git switch main && git pull
  railway config plan
  railway config apply
  ```

  The plan shows every change as a diff, with secret values redacted (`--show-values` would print
  them). Nothing changes until `apply`, which runs the plan again, asks for confirmation, and is
  rejected if the environment changed in between, by a dashboard edit or another apply.
  `railway config plan --detailed-exit-code` exits 2 when the environment differs from the file.
- **Settings outside the file** (["Settings outside the infrastructure
  file"](#settings-outside-the-infrastructure-file)): change them in the dashboard. Each change is
  staged, with its old and new value, until you choose "Deploy"; networking changes apply at once.
  Update the table in this guide through a pull request.
- A setting the file holds that is changed in the dashboard differs from the file, and the next plan
  shows it. Change it in the file instead, except for a rehearsal or a temporary setting that you
  reverse.
- Changes to `db` (its image, start command, size, or volume) restart the database, a short outage
  for the whole pilot. Make them at a quiet time; the monitors alert while it lasts.

## Temporary settings

The limits and alert thresholds keep the defaults of the data model ("Settings") unless a variable
sets them: `DAILY_QUESTION_LIMIT`, `DAILY_REVIEW_LIMIT`, `PILOT_DAILY_QUESTION_LIMIT`,
`PILOT_DAILY_REVIEW_LIMIT`, `RATE_LIMIT_PER_MINUTE`, `WORKER_SILENT_SECONDS`,
`JOBS_WAITING_ALERT_SECONDS`, `JOBS_FAILING_ALERT_COUNT`, `BACKUP_MISSING_HOURS`, and
`INDEXING_DEADLINE`. Set one on `api` and `worker`, without deploying:

```bash
railway variable set PILOT_DAILY_QUESTION_LIMIT=2 -s api --skip-deploys
railway variable set PILOT_DAILY_QUESTION_LIMIT=2 -s worker --skip-deploys
```

In the dashboard, review the staged changes (each variable with its old and new value), and choose
"Deploy", which redeploys both services at the running commit; the worker's gate passes at once. To
return to the default, delete the variable on both services in the dashboard (each service's
Variables, the variable's menu), review the staged changes, and deploy again. A temporary variable
is not in `.railway/railway.ts`, so `railway config plan` lists it as a deletion until you remove
it; do not apply the file while one is set.

## Managing pilot users

Only GitHub accounts on the access list can sign in; at most 10. The operator commands run in the
`api` container, and no release is needed:

```bash
railway ssh -s api -- python -m codeatlas.ops pilot-users list
railway ssh -s api -- python -m codeatlas.ops pilot-users add <login>
railway ssh -s api -- python -m codeatlas.ops pilot-users remove <login>
```

A note with spaces needs an interactive session:

```bash
railway ssh -s api
python -m codeatlas.ops pilot-users add <login> --note "<who and why>"
exit
```

- `add` resolves the login to its GitHub user ID, so a later rename keeps the access. It exits 1
  if the login does not exist, is already listed, or the list is full.
- `remove` deletes the entry and signs the user out everywhere at once; their next request gets
  the signed-out page, and signing in again shows the invitation page. It exits 1 if the login is
  not listed.
- `list` prints each login, user ID, note, and the date it was added.

Each change is recorded in the audit log (`pilot_user_add`, `pilot_user_remove`).

## Deleting a removed user's data

When a removed user asks for their data to be deleted:

```bash
railway ssh -s api -- python -m codeatlas.ops pilot-users delete-data <login>
```

It disconnects every repository of the user's workspace (hidden at once and purged within 24
hours), deletes their stored GitHub tokens, and revokes their sessions, and records
`pilot_user_delete_data` in the audit log. It exits 1 while the login is still listed (remove it
first) or when no such user exists. The data can remain in the encrypted backups for up to 10 days
after deletion, as the external processing disclosure states.

## Replacing a credential

On each service that holds the credential, change the sealed variable from its menu ("Edit"; it
stays sealed), review the staged changes, and choose "Deploy", which redeploys those services at
the running commit. Update the copy in your password manager.

| Credential | Services | Steps |
| --- | --- | --- |
| `GEMINI_API_KEY`, `VOYAGE_API_KEY` | `api`, `worker` | Create a new key in the provider's console, set it, deploy, then revoke the old key |
| `GITHUB_APP_CLIENT_SECRET` | `api`, `worker` | Generate a new client secret in the App's settings, set it, deploy, then delete the old secret |
| `GITHUB_APP_PRIVATE_KEY` | `api`, `worker` | Generate a new private key in the App's settings, set the downloaded file's content as the value, deploy, then delete the old key in the App's settings and the downloaded file |
| `GITHUB_WEBHOOK_SECRET` | `api`, `worker` | Generate one with `openssl rand -hex 32`, set it, deploy, and set it in the App's webhook settings right away. Deliveries in between fail their signature check; redeliver them from the App's Recent Deliveries page, or let the daily check find the missed pushes |
| `TOKEN_ENCRYPTION_KEY` | `api`, `worker` | Generate a new key (["Variables and secrets"](#variables-and-secrets)), set it, deploy. The stored GitHub tokens become unreadable, so every user is asked to sign in again; their repositories stay connected |
| `POSTGRES_PASSWORD` | `db` | ["Replacing the database password"](#replacing-the-database-password) |
| The bucket's keys | `backup` | ["Replacing the bucket's keys"](#replacing-the-buckets-keys) |

### Replacing the database password

PostgreSQL reads `POSTGRES_PASSWORD` only when it creates its data directory, so the password
changes in the database first, then in the variable. Do the steps back to back: until the
deployment, new connections from `api`, `worker`, and `backup` fail.

1. Generate a new password with `openssl rand -hex 24` (letters and digits only, because it becomes
   part of `DATABASE_URL`), and keep it in your password manager.
2. Change it in the database. `\password` runs `ALTER ROLE codeatlas PASSWORD '...'` with the
   password already hashed, so it appears in no log:

   ```bash
   railway ssh -s db
   psql -U codeatlas -d codeatlas
   ```

   ```text
   codeatlas=# \password codeatlas
   Enter new password for user "codeatlas":
   Enter it again:
   codeatlas=# \q
   ```

   Then `exit` the session.
3. Change `POSTGRES_PASSWORD` on `db` to the new password (the variable's menu, "Edit"). The
   reference variables carry it into `DATABASE_URL` on `api`, `worker`, and `backup`.
4. Review the staged changes and deploy. `db` restarts briefly and keeps its data; `api`,
   `worker`, and `backup` redeploy with the new `DATABASE_URL`.

### Replacing the bucket's keys

The bucket in `codeatlas-backups` has one key pair, and `backup` holds sealed copies of it.
Resetting the pair invalidates the old keys at once, so do the steps back to back: until the
deployment, backups fail.

1. Reset the keys, which asks for confirmation, then print the new values:

   ```bash
   (cd ~/codeatlas-backups && railway bucket credentials -b backups --reset)
   (cd ~/codeatlas-backups && railway bucket credentials -b backups)
   ```

2. On `backup` in `production`, change `BACKUP_ACCESS_KEY_ID` and `BACKUP_SECRET_ACCESS_KEY`, and
   any other of the five values that changed, from each variable's menu ("Edit"; it stays sealed).
3. Review the staged changes and deploy.
4. Run a backup by hand (["Backups"](#backups)), or check the next night's run.

## Alerts

Railway emails release and spending events. Better Stack checks the public address and the four
status endpoints from outside Railway, and emails on failure and on recovery.

### Railway notifications

In Railway's account settings, Notifications, set build failed, deployment failed, and deployment
crashed to email, with a project override for `codeatlas` if the account-wide rules differ. Each
email reports a single event, with no recovery notice:

- **Build failed**: a release's build failed.
- **Deployment failed**: a pre-deploy command (a migration or a release gate) or a healthcheck
  failed. The previous deployment keeps serving.
- **Deployment crashed**: a running service exited and used up its 10 restarts.

### Usage limits

On the workspace's Usage page, "Set Usage Limits": a custom email alert at $20 (80% of the $25
monthly budget) and a hard limit at $50, twice the budget. Or from the command line:

```bash
railway usage limit set --target workspace --soft 20 --hard 50 --workspace <workspace ID>
railway usage limit status
```

`railway status --json` reports the workspace ID as `workspaceId`.

Railway also warns at 75% and 90% of the hard limit, and at 100%, when it takes every service
offline to stop further spending. To bring the pilot back, raise or remove the hard limit; Railway
then redeploys the stopped services. Redeploy any service that does not come back, `db` first
(`railway redeploy -s <service>`, or the deployment's menu, "Redeploy"), then check
`curl -s https://<domain>/readyz` and `/version`.

### Monitors

Create six monitors in Better Stack, each alerting when its URL becomes unavailable (an answer other
than 2xx, or none), checked every 3 minutes, and emailing you on failure and on recovery:

| Monitor | URL | Confirmation period | Alerts when |
| --- | --- | --- | --- |
| Web app | `https://<domain>/` | 5 minutes | Not 2xx for 5 minutes |
| API and database | `https://<domain>/readyz` | 5 minutes | Not 2xx for 5 minutes |
| Worker | `https://<domain>/status/worker` | None | 503 or no answer |
| Waiting jobs | `https://<domain>/status/jobs-waiting` | None | 503 or no answer |
| Failing jobs | `https://<domain>/status/jobs-failing` | None | 503 or no answer |
| Backups | `https://<domain>/status/backup` | None | 503 or no answer; created after the first backup completes |

A status endpoint answers 200 or 503 with `condition`, `status`, `value`, and `threshold`, and 503
`not_ready` when the database is unreachable. Each condition already includes its duration (the
worker silent for 5 minutes, a job waiting 30 minutes), so its monitor alerts on the first 503.
`/status/backup` fails until the first nightly backup completes, so create its monitor once
`backup`'s logs show `backup_completed`, or `curl -s https://<domain>/status/backup` reports
`"status": "ok"`. To get there sooner, run a backup off its schedule (["Backups"](#backups)). The
status paths are rate-limited per client address, at 60 requests a minute; the monitors stay far
below it.

### When an alert arrives

| Alert | Means | Look first at |
| --- | --- | --- |
| Web app | `/` has failed for 5 minutes | `railway deployment list -s web`, `railway logs -s web --lines 100`, and Railway's status page |
| API and database | `/readyz` has failed for 5 minutes: `api` or `db` is down, or `web` cannot reach `api` | `railway logs -s api --lines 100`, `railway logs -s db --lines 100`, and `db`'s volume (["Metrics"](#metrics)) |
| Worker | No heartbeat for 5 minutes | `railway logs -s worker --lines 100`; then `railway redeploy -s worker` |
| Waiting jobs | A job that could run has waited 30 minutes | The `worker_status` lines (["Logs"](#logs)): a stuck job, or more work than one worker handles |
| Failing jobs | 3 counted failures (retries used up, timeouts, internal errors) within an hour; the monitor stays failing for an hour after the third | The failed jobs' `job_finished` lines, then each job's lines by `@job_id`, and provider errors by `@provider` |
| Backups | No backup has completed in 26 hours | `backup`'s logs (["Backups"](#backups)) |
| Build failed, deployment failed | A release step failed; the previous version serves | The deployment's logs: `railway logs -s <service> --latest`, or with `--build` |
| Deployment crashed | A service exited and used up its restarts | `railway logs -s <service> --lines 200`; then redeploy it |
| Usage | Spending passed $20, or the hard limit | `railway usage` and the services' metrics |

## Logs

Railway keeps every service's standard output and error for 7 days. Its Log Explorer shows every
service of an environment in one view and parses the JSON lines of `api`, `worker`, and `backup`,
so each field is a filter. Every API response has an `X-Request-ID` header, and error bodies repeat
it as `request_id`.

| To find | Filter |
| --- | --- |
| One service's lines | `@service:<name>` |
| Errors | `@level:error` |
| A request, a job, a run, or a snapshot | `@request_id:<id>`, `@job_id:<id>`, `@run_id:<id>`, `@snapshot_id:<id>` |
| API requests (`method`, `route`, `status`, `duration_ms`) | `request`, for example `request AND @status:>=500` |
| Finished jobs (`kind`, `outcome`, `attempt`, `duration_ms`) | `job_finished`, for example `job_finished AND @outcome:failed` |
| Model provider errors | `@provider:gemini`, `@provider:voyage` |
| The queue every 60 seconds (`queued_jobs`, `oldest_runnable_job_age_seconds`) | `worker_status` |
| Releases | `migrations_skipped` (`database_revision`, `release_revision`); `release_gate_waiting` (`commit`, `reported_commit`) |
| Backups | `backup_completed` (`object_key`, `bytes`); `backup_pruned` (`pruned`) |
| Requests at the edge | The HTTP logs, by `@httpStatus` and `@totalDuration`, for example `@httpStatus:>=500` or `@totalDuration:>=1000` |

The same searches from the command line, one service at a time:

```bash
railway logs -s api --since 1h --filter "@request_id:<id>"
railway logs -s worker --since 1d --filter "job_finished AND @outcome:failed"
railway logs -s worker --since 1h --filter "worker_status"
railway logs -s web --http --status ">=500" --since 1h
railway logs -s web --http --filter "@totalDuration:>=1000" --since 1h
railway logs -s api --since 7d --json > api-logs.json    # an export, for a credential scan
```

No application line contains a token, source text, a prompt, or model output: the logger writes
only an allow-list of fields. Whether the edge's HTTP logs hold the sign-in callback's query string
(`code` and `state`) is recorded in the quickstart (scenario 11).

## Metrics

Each service's Metrics tab in the dashboard shows its CPU, memory, disk, and network for up to 30
days, and `db`'s shows its volume. From the command line:

```bash
railway metrics --all --since 7d                       # every service
railway metrics -s worker --since 1d --cpu --memory
railway metrics -s db --volume                         # the database's storage
railway metrics -s web --http --since 1d               # requests, errors, and latency at the edge
```

Request numbers by route come from the API's `request` lines; job durations, retries, and failures
by kind from the `job_finished` lines; and the queue from the `worker_status` lines
(["Logs"](#logs)). There is no dashboard of our own.

The Hobby plan caps the database volume at 5 GB. When it passes 3 GB (ADR 0014), plan the move to
the Pro plan, which can grow the volume. Indexing that cannot finish for lack of space fails with a
stated reason, and repeated failures raise the failing-jobs alert.

## Backups

Every night at 03:30 UTC, the cron service `backup` runs `python -m codeatlas.ops backup` and
exits; Railway skips a run while the previous one is still active. Its restart policy is "never": a
failed run waits for the next night, and the backup alert fires once 26 hours pass without a
completed backup. Each run:

1. **Prunes first**: deletes from the bucket every backup dated before the last 7 UTC days
   (`BACKUP_RETENTION_DAYS`, today included, so dates from 6 days ago on are kept), then deletes
   their `backups` rows if the database answers, and logs `backup_pruned`. Pruning runs whatever
   happens next, so a failing backup does not keep old backups alive.
2. Dumps the database with `pg_dump -Fc` from an exported snapshot, and in the same snapshot counts
   the rows of every table and reads the Alembic revision, so the counts describe exactly the dump.
3. Checks the dump with `pg_restore --list`, and computes its size and SHA-256.
4. Uploads `backups/<UTC date>/<UTC time>/codeatlas.dump`, then `manifest.json` beside it (the
   commit, the Alembic revision, the dump's key, size, and SHA-256, and the row counts). The time
   (`HHMMSS`) gives every run its own keys; a run stops if they already exist.
5. Inserts a `backups` row and logs `backup_completed`.

Any failure after pruning exits 1 with no row. A dump without its manifest is incomplete: restore
ignores it, and pruning removes it with its date.

- **Where**: the bucket `backups` of the project `codeatlas-backups`, private and encrypted at
  rest. It is in the same Railway account as the database but not in the same project, so losing
  the pilot's environment or project leaves the backups, and losing the account loses both.
  Uploads cross the public network. The bucket's one key pair has full access, so `backup` could
  also delete backups.
- **Retention**: 7 days. Deleted data is purged from the database within 24 hours, so it can remain
  in a backup for up to 10 days after deletion, as the external processing disclosure states. The
  bound holds while the nightly runs happen.

Check the recent runs:

```bash
railway logs -s backup --since 2d
railway logs -s backup --since 7d --filter "backup_completed OR backup_pruned"
```

The bucket's Files view in the dashboard (project `codeatlas-backups`) lists the stored backups.
The completed ones are also in the database:

```bash
railway ssh -s db
psql -U codeatlas -d codeatlas \
  -c 'SELECT object_key, completed_at, bytes, release FROM backups ORDER BY completed_at DESC;'
exit
```

To run a backup by hand, for example after fixing a failure, after a restore, or before the first
night, start the cron service off its schedule, with its own variables:

```bash
railway cron run -s backup -y
railway logs -s backup --latest
```

The pilot's first backup ran this way: it stored `backups/2026-10-12/212351/codeatlas.dump`
(61,337 bytes), and `/status/backup` turned from 503 to 200. In a restore session that has the
bucket's keys exported (["Restoring a backup"](#restoring-a-backup), step 3),
`python -m codeatlas.ops backup` runs the same command. A second run on the same day stores a
second backup under a new time and leaves the first untouched.

## Restoring a backup

`ops restore` replaces the database with one backup. Everything written after that backup is lost,
so restore into the pilot only when its data is gone or unusable. It runs in the `api` container,
with the bucket's keys exported for that session only, so `api` holds them only while you restore.

1. Choose the backup: a UTC date (for example `2026-10-13`) restores the newest complete backup of
   that day, and `<date>/<time>` (for example `2026-10-13/033012`) a specific one. The bucket's
   Files view lists them.
2. Close the pilot: stop `web`, the only public entry, so that nothing writes through the API, and
   `worker`. The monitors alert; that is expected.

   ```bash
   railway down -s web
   railway down -s worker
   ```

3. Print the bucket's keys on your machine (or open the bucket's Credentials tab), then open a
   session in `api` and copy them in:

   ```bash
   (cd ~/codeatlas-backups && railway bucket credentials -b backups)
   railway ssh -s api
   ```

   In the session, `read` keeps the values out of the shell history, and the secret key is not
   echoed:

   ```bash
   read -r BACKUP_ENDPOINT_URL                                # AWS_ENDPOINT_URL
   read -r BACKUP_REGION                                      # AWS_DEFAULT_REGION
   read -r BACKUP_BUCKET                                      # AWS_S3_BUCKET_NAME
   read -r BACKUP_ACCESS_KEY_ID                               # AWS_ACCESS_KEY_ID
   stty -echo; read -r BACKUP_SECRET_ACCESS_KEY; stty echo    # AWS_SECRET_ACCESS_KEY
   export BACKUP_ENDPOINT_URL BACKUP_REGION BACKUP_BUCKET BACKUP_ACCESS_KEY_ID \
     BACKUP_SECRET_ACCESS_KEY
   python -m codeatlas.ops restore <backup>
   exit
   ```

   `restore` downloads the dump and its manifest and checks the dump's SHA-256 against the
   manifest, changing nothing if it differs. It asks you to type `yes` (unless `--yes`), ends
   every other database session, drops and recreates the `public` schema, runs
   `pg_restore --no-owner --exit-on-error`, and compares the restored Alembic revision and row
   counts with the manifest, printing each difference. Last, it migrates the schema to this
   release's revision, as `migrate` does, so a backup taken before a schema-changing release
   works too. It exits 0 when the backup is restored, verified, and migrated, and 1 on a missing
   setting (named, without its value), a missing backup, a checksum mismatch, a restore error, a
   verification difference, or a failed migration. Leaving the session discards the keys.
4. Open the pilot again:

   ```bash
   railway redeploy -s worker
   railway redeploy -s web
   ```

   If a service does not come back this way, open its Deployments in the dashboard, and choose
   "Redeploy" or "Rollback" on the removed deployment.
5. Check `curl -s https://<domain>/readyz`, sign in, and look at a repository, an answer, and a
   review.
6. Tell the pilot users the backup's time (`created_at` in its manifest, or the date and time in its
   key): questions, reviews, and repository changes after it are lost, and repositories they
   disconnected after it are connected again, so they should disconnect those again. Lost
   repository access is found again by the daily access check.

A backup's own `backups` row is written after its dump, so after a restore, the newest row is the
one before the restored backup. `/status/backup` may therefore fail until the next nightly run.
Expect the alert, or run a backup by hand (["Backups"](#backups)) once the pilot is open again.

## Recovery exercise

The drill rebuilds the pilot's environment from the recorded configuration and the latest backup,
to prove that recovery works and to time it (SC-008: within 2 hours, losing only changes made after
the backup). The drill never runs the worker, so no background job acts on the restored data. Do
not merge to `main` while it runs: the drill deploys `main` too, and would start its worker.
Record the start time, then:

1. Duplicate the pilot. Its services are staged, not deployed:

   ```bash
   railway environment new drill --duplicate production
   ```

2. Set the secrets in `drill`, as in ["Variables and secrets"](#variables-and-secrets) with
   `-e drill`: the pilot's values on `api` and `worker`, from your password manager, and a new
   `POSTGRES_PASSWORD` on `db`. Seal them. Find the drill's domain, `<drill domain>`, with
   `railway domain list -s web -e drill`; if `web` has none there, generate one with
   `railway domain -s web -e drill --port 3000`.
3. Review the drill's staged changes in the dashboard, and deploy them. Wait until `api` has passed
   its healthcheck (`railway deployment list -s api -e drill`); it migrates the empty database.
4. Remove the worker's deployment, and stop the web app:

   ```bash
   railway down -s worker -e drill
   railway down -s web -e drill
   ```

5. Restore the pilot's latest backup in the drill's `api`, as in ["Restoring a
   backup"](#restoring-a-backup), step 3, but open the session with `railway ssh -s api -e drill`.
   The keys you export are those of the pilot's bucket in `codeatlas-backups`. Do not run a backup
   in this session: it would write the drill's database to the pilot's bucket.
6. Start the web app:

   ```bash
   railway environment drill
   railway redeploy -s web
   railway environment production
   ```

7. In the pilot GitHub App's settings (General, Callback URL, "Add Callback URL"), add
   `https://<drill domain>/auth/github/callback`.
8. Sign in at `https://<drill domain>`, and check your repositories, an earlier answer, and an
   earlier review. Do not submit new questions or reviews: without the worker they only queue.
9. Record the end time and the result in the quickstart's validation record.
10. Remove the drill's callback URL from the App, and delete `drill` (["Tearing
    down"](#tearing-down)).

## Losing the database volume

When `db`'s volume is lost or its data cannot be used, the pilot comes back from the latest backup,
and changes made after it are lost: up to 24 hours, more if a failed nightly backup was not fixed.
A crashed or restarted `db` is not this case; it returns by itself with its data.

1. Find the latest backup in the bucket's Files view (project `codeatlas-backups`), and note its
   date and time; the `backups` table went with the volume.
2. Close the pilot: `railway down -s web` and `railway down -s worker`.
3. Replace the volume: delete the lost one if it still exists, then apply the recorded
   configuration, which declares `db`'s volume at `/var/lib/postgresql/data`:

   ```bash
   railway volume list
   railway volume delete --volume <the lost volume>
   railway config plan    # adds db's volume
   railway config apply
   ```

   `db` starts on the empty volume and creates a new database with the same `POSTGRES_PASSWORD`.
   If it does not redeploy by itself, run `railway redeploy -s db`.
4. Wait until `db` is running (`railway logs -s db --lines 20`). `api` needs no redeploy, because
   `restore` replaces the whole schema.
5. Restore the backup and open the pilot again: ["Restoring a backup"](#restoring-a-backup), steps
   3 to 6.

## Losing the environment or project

A Railway outage takes the pilot down until Railway recovers. The monitors run outside Railway, so
they alert, and nothing needs rebuilding. When the `production` environment or the whole project
is deleted or unusable, rebuild it from `.railway/railway.ts`:

1. Record the start time, and tell the pilot users.
2. Create what is gone, then link to it:
   - the project: `railway init --name codeatlas`, which comes with a `production` environment; or
   - the environment only: `railway environment new production`.

   ```bash
   railway link --project codeatlas --environment production
   ```

3. Follow ["One-time setup"](#one-time-setup) from ["The project"](#the-project) (skipping
   `railway init`, and ["The backups bucket"](#the-backups-bucket), which survived) to ["The first
   deployment"](#the-first-deployment), with the secrets from your password manager, a new
   `POSTGRES_PASSWORD`, and the bucket's five values printed again. For the domain, take the old
   name back as in ["The domain"](#the-domain)
   (`railway domain update <domain ID> -s web --domain codeatlas-pilot.up.railway.app`) if it is
   free. Otherwise keep the new name: change the pilot App's homepage, callback, and webhook URLs,
   the monitors' URLs, and the table in this guide, and tell the pilot users the new address.
4. Restore the latest backup from `codeatlas-backups`: ["Restoring a
   backup"](#restoring-a-backup), steps 1 to 6. The new environment's `api` migrated an empty
   database; the restore replaces it, pilot users included.
5. The new environment has no `backups` rows until its first nightly run, so `/status/backup`
   fails until then; run a backup by hand (["Backups"](#backups)) to end the alert sooner.
6. Record the end time.

Only losing the Railway account loses the backups too (see the README's known limitations). The
pilot then starts empty: rebuild both projects as in ["One-time setup"](#one-time-setup), add the
pilot users again, and ask them to connect their repositories again.

## Load test

The load test (SC-011, research R15) measures 001 SC-007's latency targets at the pilot's service
sizes; the 10-user level must meet them. It runs against `loadtest`, a copy of the pilot's
environment: the same services, sizes, and commit, with an empty database, its own domain, fake
externals, raised limits, no rate limit, and no secrets except a new token encryption key and
database password. The `Load test` workflow (`.github/workflows/load-test.yml`) runs
`evals/perf_check.py` from a GitHub-hosted runner: 10 concurrent users, then 20, 40, and 80, for
120 seconds each, stopping after the first level that misses a target or returns errors. The
environment costs money while it exists, so build, run, and delete it in one sitting, and do not
merge to `main` meanwhile.

1. Duplicate the pilot. Its services are staged, not deployed:

   ```bash
   railway environment new loadtest --duplicate production
   ```

2. Set its variables before the first deployment, on `api` and `worker`:

   ```bash
   for service in api worker; do
     railway variable set CODEATLAS_ENV=development CODEATLAS_FAKE_EXTERNALS=1 \
       DAILY_QUESTION_LIMIT=100000 PILOT_DAILY_QUESTION_LIMIT=100000 \
       PILOT_DAILY_REVIEW_LIMIT=1000 RATE_LIMIT_PER_MINUTE=0 \
       -s "$service" -e loadtest --skip-deploys
   done
   ```

   Set a new `TOKEN_ENCRYPTION_KEY` on both and a new `POSTGRES_PASSWORD` on `db` (["Variables and
   secrets"](#variables-and-secrets), with `-e loadtest`), and seal them. In the dashboard, clear
   `backup`'s cron schedule in `loadtest`, so that it takes no backups. Find its domain,
   `<loadtest domain>`, with `railway domain list -s web -e loadtest`.
3. Review the staged changes and deploy them. Check that it runs the pilot's commit:

   ```bash
   curl -s https://<domain>/version
   curl -s https://<loadtest domain>/version
   ```

4. Dispatch the workflow from `main`. `base_url` is the environment's address, the scheme and host
   only (it must equal its `APP_ORIGIN`). Keep the default `levels` and `duration` unless there is a
   reason to change them, and state any change in the report:

   ```bash
   gh workflow run load-test.yml --ref main -f base_url=https://<loadtest domain> \
     -f levels=10,20,40,80 -f duration=120
   sleep 5
   RUN_ID="$(gh run list --workflow load-test.yml --limit 1 \
     --json databaseId --jq '.[0].databaseId')"
   gh run watch "$RUN_ID"
   ```

   The run's Summary page shows the network baseline (the median connection and TLS times of 20
   requests to `/version`) and the table. A level that misses a target ends the run there with a
   notice, and the job still succeeds; it fails only when `perf_check` cannot set up (exit status
   2), for example because `base_url` is not the environment's address. The step's log names the
   cause.
5. Download the artifact (`backend/evals/out/` is ignored by Git):

   ```bash
   gh run download "$RUN_ID" --name load-test --dir backend/evals/out/load-test
   ```

   It holds `level-<users>.json` for each level that ran (with the commit from `/version`),
   `network.txt` and `network.md` with the baseline, and `load-test.md` with the baseline and the
   table.
6. Read each service's peak CPU and memory during each level. The log has each level's start; a
   level ends when the next one starts, or when the step ends:

   ```bash
   gh run view "$RUN_ID" --log | grep -E 'Level: [0-9]+ users'    # each level's start time
   gh run view "$RUN_ID" --json startedAt,updatedAt              # the run's start and end
   for service in web api worker db; do
     railway metrics -s "$service" -e loadtest --since <start> --until <end> --cpu --memory --raw
   done
   railway metrics -s web -e loadtest --since <start> --until <end> --http
   ```

   A level's peak is the highest value within it; `--http` adds the latency at the edge.
7. Delete `loadtest` (["Tearing down"](#tearing-down)).
8. Write `docs/reports/load-test.md`: the steps and inputs to rerun it (the workflow inputs, the
   variables above, and the commit); the service sizes; the configuration (fake mode, the raised
   limits, no rate limit); the duration; the network baseline, noting that the runner's region is
   not guaranteed; each level's p95 latency and error rate by category; the highest level that
   meets every target, or "at least 80" when every level does; and each service's peak CPU and
   memory per level. The worker's memory peak decides whether its 2 GB limit can come down.

## Fault tests

The fault tests (SC-007) prove that each alert arrives within 10 minutes of its condition (the
backup alert within an hour after the 26-hour mark), and that a recovery email follows where one is
expected. They disturb the pilot, so run them at a quiet time, and record when each fault starts
and when each email arrives in the quickstart's validation record.

| Fault | Start it | Expected alert | End it |
| --- | --- | --- | --- |
| Web app down | `railway down -s web` | The web app and API monitors fail after 5 minutes | `railway redeploy -s web`; both recover |
| Worker down with jobs queued | `railway down -s worker`, then re-index a repository in the web app | The worker monitor fails 5 to 8 minutes after the last heartbeat | `railway redeploy -s worker`; it recovers |
| Failing jobs | Set `INDEXING_DEADLINE=5` (seconds) on `worker` (["Temporary settings"](#temporary-settings)), deploy, and re-index three repositories that take longer | The failing jobs monitor fails on the third timeout | Remove the variable and deploy; it recovers an hour after the third failure |
| Waiting jobs | With the worker down, keep a job queued for 30 minutes, or set `JOBS_WAITING_ALERT_SECONDS=60` on `api` to prove the path quickly | The waiting jobs monitor fails | `railway redeploy -s worker`, and remove the variable; it recovers |
| Failed release | ["Rehearsing a failed release"](#rehearsing-a-failed-release), the failed readiness check | Railway's deployment-failed email | Restore the healthcheck path |
| Backup skipped | Clear `backup`'s cron schedule in the dashboard and deploy, until 26 hours after the last backup | The backups monitor fails within an hour after the mark | Restore `30 3 * * *` and deploy; run a backup by hand, or wait for the next night; it recovers |

## Tearing down

### A temporary environment

```bash
railway environment delete drill    # or loadtest; asks for confirmation
```

This deletes the environment's services, volume, and domain. After a drill, also remove its
callback URL from the pilot App.

### The pilot

Deleting the project `codeatlas` deletes the database at once, permanently. The backups stay in
`codeatlas-backups` until you delete that project, which deletes them permanently; without the
pilot nothing prunes them, so delete it too.

1. Tell the pilot users, and remove the App's installations or ask them to.
2. Delete the Better Stack monitors.
3. Delete both projects; each asks for confirmation:

   ```bash
   railway delete --project codeatlas
   railway delete --project codeatlas-backups
   ```

4. Delete the pilot GitHub App (its settings, Advanced, Delete GitHub App), and revoke the model
   provider keys that only the pilot used.
5. Remove the usage limits if no other project needs them (`railway usage limit remove`), and the
   SSH key once you no longer use `railway ssh` (`railway ssh keys remove`).
