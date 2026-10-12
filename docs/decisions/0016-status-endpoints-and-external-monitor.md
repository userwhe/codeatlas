# ADR 0016: Status endpoints checked by an external monitor

- **Status**: Accepted
- **Date**: 2026-10-12
- **Scope**: `specs/004-pilot-deployment`; supersedes ADR 0012

## Context

The developer must be emailed, with a recovery notice, when the public address fails, the worker
stops, jobs wait 30 minutes, 3 jobs fail within an hour, or no backup completes in 26 hours, and
also when a release fails or spending passes 80% of the budget. Railway (ADR 0014) emails failed
builds, failed deployments, crashes, and spending thresholds, but it checks a service's health only
during a deployment, and it cannot see the queue, the worker's silence, or the backups. A check
that runs on the same platform would fail with it.

## Options considered

1. Status endpoints in the API, one per condition, backed by the database, and a free external
   uptime monitor that checks them and the public address.
2. Heartbeat monitors: the worker and the backup ping a monitoring service's URL.
3. A checker inside Railway (a cron service) that sends email through a mail provider.
4. Prometheus and Grafana as services.

## Decision

Option 1:

- `GET /status/worker`, `/status/jobs-waiting`, `/status/jobs-failing`, and `/status/backup`
  return 200 or 503 with the measured value and threshold, and 503 when the database is
  unreachable. They are public, rate-limited per client address, and served through the web app.
- Their data: a new `worker_heartbeats` table, which the worker's reporter thread updates every
  60 seconds; the queue's own `runnable_waiting`; a new `jobs.error_permanent` column, which keeps
  the earlier rule for counting failures; and a new `backups` table, written after each upload.
  The migration is `0005_operations_status`.
- The failing-jobs condition holds for an hour after the third counted failure within 60 minutes,
  so a run of failures cannot fall between two checks.
- Better Stack's free plan checks the four endpoints, `/`, and `/readyz` every 3 minutes and emails
  on failure and recovery; `/` and `/readyz` alert after a 5-minute confirmation period.
- Metrics come from Railway (CPU, memory, disk, network, HTTP) and from structured log lines: the
  worker writes its queue numbers every minute. The CloudWatch embedded metric format and its
  settings are removed, and so are the disk and certificate alerts.

## Trade-off

- **Gained**:
  - Each condition is a query in tested code; the monitor needs no credentials and costs nothing.
  - The check runs outside the platform, so a platform outage still alerts.
- **Accepted**:
  - Anyone can read four operational numbers; they reveal no user data.
  - Alerts arrive up to one check interval, 3 minutes, after a condition; the monitoring account is
    one more external service.
  - The worker writes a heartbeat row every minute.
  - No dashboard of our own; queue and job numbers are read from the logs.
- **Revisit when**:
  - the monitors outgrow the free plan's 10;
  - an alert is needed on a number that only the logs hold; or
  - the status endpoints draw abusive traffic: require a token in the monitor's request.

Details: `specs/004-pilot-deployment/research.md` R9 and R10.
