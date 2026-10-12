// Railway infrastructure as code for the pilot: the `production` environment of the `codeatlas`
// project, in US East. It records the services, the database volume, and their variables
// (specs/004-pilot-deployment/contracts/operations.md). The backups bucket is not here: it lives in
// a separate project, `codeatlas-backups`, so that losing this environment or project does not
// lose the backups (research R12).
//
// The developer applies it from their own machine, signed in with the Railway CLI; CI holds no
// Railway token:
//
//   npm ci --prefix .railway   # installs the `railway` package this file imports
//   railway config plan        # read-only: the diff against the environment, values redacted
//   railway config apply       # plans again, asks for confirmation, then applies
//
// Secrets are declared with preserve(), so no value is in this file: the developer sets each one
// with the CLI and seals it in the dashboard, and apply keeps it. A variable set on Railway
// but not declared here appears in the next plan as a destructive deletion. Settings this file
// cannot hold (the generated domain, notification rules, usage limits, SSH keys, and the separate
// backups project) are recorded in docs/operations.md. Railway's GitHub App must be able to read the
// repository before the first apply, or the services get no deployment trigger.

import { defineRailway, github, image, preserve, project, service, volume } from "railway/iac";

const repository = "userwhe/codeatlas";

// US East (Virginia): the region identifier for services and volumes.
const region = "us-east4-eqdc4a";

const oneReplicaInRegion = { [region]: 1 };

// Every app service deploys from `main` with "Wait for CI" (`checkSuites`): a deployment waits
// until the commit's GitHub Actions check suites pass, and is skipped if one fails (research R7).
function fromMain(rootDirectory: string) {
  return github(repository, { branch: "main", rootDirectory, checkSuites: true });
}

// A crashed service restarts by itself, up to 10 times (research R2): that is Railway's default
// restart policy, which the platform does not report back, so the file leaves it unset except for
// `backup`. Railway also builds with the Dockerfile in a service's root directory by itself; its API
// has no Dockerfile builder to set, so the file names no builder either.

// The release gate waits up to 20 minutes for `api` to report the deployment's commit; the
// pre-deploy timeout leaves room above that (research R7).
const releaseGateTimeout = { preDeployTimeoutSeconds: 1_500 };

// Replica limits: vCPU, and memory in bytes (decimal gigabytes).
function sizeLimit(cpu: number, memoryGB: number) {
  return { limitOverride: { containers: { cpu, memoryBytes: memoryGB * 1_000_000_000 } } };
}

// Values that embed another service's variable use Railway's reference syntax, which the platform
// resolves at deploy time, so a duplicated environment gets its own domains and password.
const appOrigin = "https://${{web.RAILWAY_PUBLIC_DOMAIN}}";
const databaseUrl =
  "postgresql+psycopg://codeatlas:${{db.POSTGRES_PASSWORD}}@${{db.RAILWAY_PRIVATE_DOMAIN}}:5432/codeatlas";
const apiOrigin = "http://${{api.RAILWAY_PRIVATE_DOMAIN}}:8000";

// The settings `api` and `worker` share (research R5). The limits and thresholds stay unset, so the
// application's defaults apply.
const applicationSettings = {
  CODEATLAS_ENV: "production",
  CODEATLAS_FAKE_EXTERNALS: "0",
  APP_ORIGIN: appOrigin,
  DATABASE_URL: databaseUrl,
  // The pilot GitHub App's identifiers are not secret, but the App is registered only after the web
  // app's domain exists, so the developer sets them on Railway and this file keeps them.
  GITHUB_APP_ID: preserve(),
  GITHUB_APP_SLUG: preserve(),
  GITHUB_APP_CLIENT_ID: preserve(),
  // Sealed secrets.
  GITHUB_APP_CLIENT_SECRET: preserve(),
  GITHUB_APP_PRIVATE_KEY: preserve(),
  GITHUB_WEBHOOK_SECRET: preserve(),
  TOKEN_ENCRYPTION_KEY: preserve(),
  GEMINI_API_KEY: preserve(),
  VOYAGE_API_KEY: preserve(),
};

export default defineRailway(() => {
  // PostgreSQL 17 with pgvector, reachable only on the private network: no public domain and no
  // TCP proxy (research R1).
  // 5 GB, the Hobby plan's cap, which Railway gives the volume when it is created.
  const dbData = volume("db-data", { region, sizeMB: 5_000 });
  const db = service("db", {
    source: image("pgvector/pgvector:pg17"),
    // On Railway a start command replaces the image's ENTRYPOINT, so it runs the image's entrypoint
    // script, which initializes the data directory on first start and drops root privileges.
    start: "docker-entrypoint.sh postgres -c shared_buffers=256MB -c max_connections=50",
    replicas: oneReplicaInRegion,
    deploy: { ...sizeLimit(1, 1) },
    volumeMounts: { "/var/lib/postgresql/data": dbData },
    env: {
      POSTGRES_USER: "codeatlas",
      POSTGRES_DB: "codeatlas",
      // Sealed, generated once. DATABASE_URL embeds it in a URL, so it uses URL-safe characters.
      POSTGRES_PASSWORD: preserve(),
      // A subdirectory, because the volume's root holds lost+found, which initdb refuses.
      PGDATA: "/var/lib/postgresql/data/pgdata",
    },
  });

  // The Next.js server from the last stage of frontend/Dockerfile, which runs `node server.js`.
  // The only public service, on the domain the developer generates.
  const web = service("web", {
    source: fromMain("frontend"),
    preDeploy: "node scripts/wait-for-api.mjs",
    healthcheck: "/",
    replicas: oneReplicaInRegion,
    deploy: { ...releaseGateTimeout, ...sizeLimit(0.5, 0.5) },
    env: {
      // A build argument for the rewrites, and the release gate's target at run time.
      API_ORIGIN: apiOrigin,
      PORT: "3000",
      // The old deployment keeps answering for 10 seconds after the switch, then gets 10 seconds
      // between SIGTERM and SIGKILL (research R7).
      RAILWAY_DEPLOYMENT_OVERLAP_SECONDS: "10",
      RAILWAY_DEPLOYMENT_DRAINING_SECONDS: "10",
    },
  });

  // The API. Its pre-deploy command migrates the database before the new version starts, and
  // traffic moves only after /readyz passes (research R7).
  const api = service("api", {
    source: fromMain("backend"),
    start: "uvicorn codeatlas.api.app:app --host 0.0.0.0 --port 8000",
    preDeploy: "python -m codeatlas.ops migrate",
    healthcheck: "/readyz",
    replicas: oneReplicaInRegion,
    deploy: { ...sizeLimit(1, 1) },
    env: {
      ...applicationSettings,
      PORT: "8000",
      CLIENT_ADDRESS_HEADER: "X-Real-IP",
      RAILWAY_DEPLOYMENT_OVERLAP_SECONDS: "10",
      RAILWAY_DEPLOYMENT_DRAINING_SECONDS: "10",
    },
  });

  // The job worker. It has no healthcheck, so the old worker is stopped as soon as the new one
  // starts; it gets 30 seconds after SIGTERM to finish its current job (research R7).
  const worker = service("worker", {
    source: fromMain("backend"),
    start: "python -m codeatlas.jobs.worker",
    preDeploy: "python -m codeatlas.ops wait-for-api",
    replicas: oneReplicaInRegion,
    deploy: { ...releaseGateTimeout, ...sizeLimit(1, 2) },
    env: {
      ...applicationSettings,
      API_ORIGIN: apiOrigin,
      RAILWAY_DEPLOYMENT_DRAINING_SECONDS: "30",
    },
  });

  // Nightly logical backups to the private bucket `backups` of the separate project
  // `codeatlas-backups` (research R12). Only the backup service holds the bucket's credentials,
  // which the developer copies from that bucket and seals here.
  const backup = service("backup", {
    source: fromMain("backend"),
    start: "python -m codeatlas.ops backup",
    preDeploy: "python -m codeatlas.ops wait-for-api",
    replicas: oneReplicaInRegion,
    deploy: {
      cronSchedule: "30 3 * * *", // UTC
      ...releaseGateTimeout,
      // A failed run waits for the next night; the backup alert fires after 26 hours.
      restartPolicyType: "NEVER",
      ...sizeLimit(0.5, 0.5),
    },
    // Only what `ops backup` uses: no CODEATLAS_ENV and none of the application's secrets.
    env: {
      DATABASE_URL: databaseUrl,
      API_ORIGIN: apiOrigin,
      BACKUP_ENDPOINT_URL: preserve(),
      BACKUP_REGION: preserve(),
      BACKUP_BUCKET: preserve(),
      BACKUP_ACCESS_KEY_ID: preserve(),
      BACKUP_SECRET_ACCESS_KEY: preserve(),
      BACKUP_RETENTION_DAYS: "7",
    },
  });

  return project("codeatlas", {
    resources: [web, api, worker, backup, db, dbData],
  });
});
