# ADR 0012: CloudWatch monitoring with a few custom metrics and log queries

- **Status**: Accepted
- **Date**: 2026-10-07
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
- Ten alarms, each notifying on alarm and on recovery, plus AWS Budgets alerts on actual and
  forecast spend. Daily metrics are checked as 26 one-hour periods that must all breach, because
  CloudWatch evaluates longer alarms only hourly and ignores the missing-data setting when enough
  real data points exist.
- Two HTTPS Route 53 health checks, of `/readyz` (the API and the database) and `/` (the web app),
  provide the outside view.

## Trade-off

- **Gained**:
  - Monitoring costs about $2 a month (the health checks' HTTPS option); the rest is in the free
    tier.
  - Logs, metrics, and alarms live outside the host, so they report its failure.
  - Containers need no credentials and no metrics library.
- **Accepted**:
  - Dashboard numbers from log queries are computed on view and cannot drive alarms.
  - With the worker stopped, the queue metrics stop too; the heartbeat alarm covers that case.
  - No tracing; identifiers in the logs connect a request to its jobs.
- **Revisit when**:
  - an alarm is needed on a number that comes only from logs: promote it to a custom metric;
  - log volume leaves the free tier; or
  - a request path spans more services than the API and the database: add tracing.

Details: `specs/004-pilot-deployment/research.md` R9 and R10.
