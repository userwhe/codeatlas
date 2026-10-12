# ADR 0012: CloudWatch monitoring with a few custom metrics and log queries

- **Status**: Superseded by [ADR 0016](0016-status-endpoints-and-external-monitor.md) on
  2026-10-12, before anything was deployed. The code it describes is in the history at commit
  `c0b0e22`.
- **Date**: 2026-10-07 (revised 2026-10-09: health checks only in `us-east-1`)
- **Scope**: `specs/004-pilot-deployment`

## Context

The developer must be emailed when the pilot is unreachable, jobs pile up or keep failing, disk runs
low, a backup is missing, the certificate nears expiry, or spending trends past the budget. Logs
from every service must be searchable for 7 days, and the last 7 days of request, job, provider, and
host metrics must be viewable in one place. Monitoring must fit the $40 hosting cap and should
survive the loss of the host it watches.

## Options considered

1. CloudWatch: container logs through Docker's `awslogs` driver, a few custom metrics through the
   embedded metric format (EMF) and host scripts, everything else from Logs Insights queries,
   alarms to SNS email, and Route 53 health checks.
2. CloudWatch with a custom metric for every route, job kind, and provider.
3. Prometheus, Grafana, and Loki on the host.
4. A third-party monitoring service.

## Decision

Option 1:

- Nine custom metrics, within the free tier's ten: worker heartbeat, queued jobs, oldest runnable
  job age, failed jobs, backup completion, certificate days left, and the CloudWatch agent's memory
  and two disk metrics. Only the pilot emits the application metrics. A reporter thread in the
  worker sends the heartbeat and queue metrics, so a long job does not silence them.
- Request latency and errors by route group, job durations and retries by kind, and provider errors
  come from Logs Insights widgets on one dashboard, over the structured JSON logs.
- Ten alarms (eight outside `us-east-1`), each notifying on alarm and on recovery, plus AWS Budgets
  alerts on actual and forecast spend. Daily metrics are checked as 26 one-hour periods that must
  all breach, because CloudWatch evaluates longer alarms only hourly and ignores the missing-data
  setting when enough real data points exist.
- Two HTTPS Route 53 health checks, of `/readyz` (the API and the database) and `/` (the web app),
  provide the outside view. Route 53 publishes their metrics only in `us-east-1`, and an alarm can
  notify only a topic in its own region, so they and their two alarms exist only when the pilot
  runs there. The pilot in `us-east-2` (ADR 0010) has the other eight alarms.

## Trade-off

- **Gained**:
  - Monitoring costs about $2 a month (the health checks' HTTPS option; nothing without them); the
    rest is in the free tier.
  - Logs, metrics, and alarms live outside the host, so they report its failure.
  - Containers need no credentials and no metrics library.
- **Accepted**:
  - Dashboard numbers from log queries are computed on view and cannot drive alarms.
  - With the worker stopped, the queue metrics stop too; the heartbeat alarm covers that case.
  - No tracing; identifiers in the logs connect a request to its jobs.
  - Outside `us-east-1`, a stopped edge proxy or web app on a running host sends no alert; a lost
    host still does, through the status check and the worker's heartbeat.
- **Revisit when**:
  - an alarm is needed on a number that comes only from logs: promote it to a custom metric;
  - log volume leaves the free tier;
  - a stopped edge proxy or web app goes unnoticed outside `us-east-1`: add an outside check in the
    pilot's region; or
  - a request path spans more services than the API and the database: add tracing.

Details: `specs/004-pilot-deployment/research.md` R9, R10, and R20.
