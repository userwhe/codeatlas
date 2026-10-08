# Quickstart and Validation: Pilot Deployment

This guide validates the pilot deployment end to end. It refers to files that the implementation
creates (see the source layout in [plan.md](plan.md)); the detailed procedures live in
`docs/operations.md`, and the interfaces in [contracts/operations.md](contracts/operations.md).
Run every path from the repository root.

Every step that writes to an external system is a developer action: Terraform applies, releases,
SSM parameters, DNS, the GitHub App and environment settings, billing, and operator commands on the
host. An agent may prepare the exact commands, plans, and summaries, and runs them only after the
developer confirms that draft; any change to it needs a new confirmation.

## Prerequisites

- An AWS account with IAM Identity Center access for the developer, and the AWS CLI v2.
- Terraform 1.11 or later.
- A domain name, delegated to the Route 53 hosted zone that `infra/shared/` creates.
- The pilot GitHub App (research R5), with the pilot's callback and webhook URLs.
- Gemini (paid tier) and Voyage AI keys, as in 001.
- For the benchmark only: a judge key (Anthropic for Claude Opus 4.5, or OpenAI for GPT-5.2) and a
  read-only GitHub token (research R16).
- For User Story 3: the repository's GitHub environment `pilot`, with the developer as required
  reviewer and `main` as the only allowed branch, and the repository's OIDC subject prefix
  (`gh api repos/<owner>/<repo>/actions/oidc/customization/sub`) in `shared.tfvars`.

## Automated checks

As in 003, plus the new infrastructure checks that CI runs:

```bash
(cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest)
(cd frontend && npm run lint && npm run typecheck && npm run build && npm run test:e2e)
terraform -chdir=infra/shared fmt -check && terraform -chdir=infra/shared validate
terraform -chdir=infra/stack fmt -check && terraform -chdir=infra/stack validate
shellcheck deploy/*.sh && bats deploy/tests
docker compose -f deploy/compose.yml config --quiet    # with placeholder runtime/*.env files, as CI creates them
docker run --rm -e CODEATLAS_HOSTNAME=example.com -v "$PWD/deploy:/etc/caddy" caddy:2.10 caddy validate --config /etc/caddy/Caddyfile
docker build --target production frontend && docker build backend
```

## First build (summary)

The operations guide has every command. For the MVP (User Story 1), in order:

1. Create the Terraform state bucket once with the AWS CLI.
2. `infra/shared/`: plan, review, apply. Delegate the domain to the zone's name servers.
3. Register the pilot GitHub App, then write the pilot's parameters with
   `deploy/put-secrets.sh pilot`.
4. `infra/stack/` with `pilot.tfvars`: plan, review, apply.
5. Release the current `main` commit by hand: build and push both images, upload the bundle, and
   run the release Run Command.
6. Add yourself and the other pilot users with `pilot-users add`.

User Story 2 later adds the alert topic (confirm the subscription email) and replaces the host so
that the new bootstrap installs the CloudWatch agent and timers. User Story 3 adds the release
role and the `pilot` environment, after which releases go through the workflow.

## Validation scenarios

| # | Scenario | Steps | Expected outcome | Covers |
| --- | --- | --- | --- | --- |
| 1 | HTTPS | `curl -sI http://<domain>/` and open `https://<domain>/` | 308 to HTTPS; a valid certificate; `Strict-Transport-Security` present | US1-1, FR-001 |
| 2 | Pilot user flow | As a listed user: sign in, install the pilot App on a test repository, connect it, ask a question, review a pull request | Each works as in 001 and 003 with the real App and providers | US1-2 |
| 3 | Automatic re-indexing | Push to the connected repository's default branch | The new commit is indexed, marked as started by a push | US1-4 |
| 4 | Not invited | Sign in with a second GitHub account that is not listed | The invitation page. No row for that account in `users`, `workspaces`, `github_credentials`, or `sessions`; one denied `sign_in` audit event | US1-3, FR-003, SC-004 |
| 5 | Removal | `pilot-users remove` a listed user who is signed in, then reload their page | 401 and the signed-out page; signing in again shows the invitation page | FR-004 |
| 6 | Pilot-wide limit | Set `PILOT_DAILY_QUESTION_LIMIT=2` with `put-secrets.sh pilot --set`, release the running commit again by hand, and submit three questions from two workspaces; then `--unset` it and release again | The third is refused with `pilot_limit_reached` and the reset time; browsing and search still work | US1-5, FR-005 |
| 7 | Rate limit | Send 70 requests to `/auth/github/login` within a minute | Requests after the 60th get 429 `rate_limited` with `Retry-After` | FR-006 |
| 8 | Startup validation | On the drill host, remove `GEMINI_API_KEY` from the rendered configuration and start `api` | It exits, naming `GEMINI_API_KEY`; no value appears in the output | US1-6, FR-009 |
| 9 | Port scan | `nmap -Pn -p- <public IP>` from outside AWS | Only 80 and 443 open | US1-7, FR-007, SC-005 |
| 10 | Readiness | Stop `db` on the drill host | `/readyz` returns 503, `/healthz` returns 200; with `db` running and `GEMINI_API_KEY` invalid, `/readyz` returns 200 | FR-011 |
| 11 | Logs | Take a request ID from a failed request's response and search it in Logs Insights; also search the Caddy log group for `code=` | Its lines from Caddy and the API; no token, source text, prompt, model output, or OAuth `code` or `state` | US2-5, FR-012 |
| 12 | Release | Merge a visible change; approve the release | Nothing is pushed to ECR before approval; the new commit in `/version` and the footer within 15 minutes of approval; the run reports the longest outage | US3-1, US3-3, SC-009 |
| 13 | Failed checks and other branches | (a) Push a commit with a failing test to a pull request branch; (b) right after a merge to `main`, cancel its CI run in the Actions page (a conclusion other than `success`), then re-run CI; (c) dispatch the release with a commit that is not on `main` | (a) No release run is created; (b) the release run for the cancelled CI has every job skipped and nothing is pushed or released, and the re-run starts a normal release; (c) the run fails in `summary` before approval | US3-2, FR-019 |
| 14 | Rollback | Dispatch the release with `expect_version=wrong` | The check fails, the previous commit serves again within 5 minutes, and an alert arrives | US3-4, FR-022, SC-010 |
| 15 | Jobs during a release | Start an indexing job, then approve a release while it runs | The job finishes, resumed by the new worker if needed; one result, no duplicate | US3-3, FR-023 |
| 16 | Environment change | Change `root_volume_gib` in `pilot.tfvars` and run `terraform plan` | The plan lists the change and nothing is applied until `terraform apply plan.out` | US3-5, FR-025 |
| 17 | Reboot | `sudo reboot` the pilot host | Every service returns without manual steps; an interrupted job resumes or fails with a reason | US2-8, FR-017 |
| 18 | No stored keys | `gh secret list` and `gh secret list --env pilot` | No AWS key | US3-6, FR-024 |
| 19 | Outside installation | Install the pilot App with an account that is not on the list, and push to one of its repositories | No job and no repository row; the delivery's `webhook_deliveries` row has outcome `ignored` and only the fields in the spec's edge case; it is purged after 14 days | Spec edge case, 002 FR-010 |

## Fault tests (SC-007)

Record when each fault starts and when the email arrives; each must arrive within 10 minutes (the
backup alert within an hour after the 26-hour mark), and a recovery email must follow.

| Fault | How | Expected alert |
| --- | --- | --- |
| Edge proxy down | `codeatlas-compose stop caddy` | `site-down` and `web-down` |
| Web app down | `codeatlas-compose stop web` | `web-down` |
| Worker down with jobs queued | `codeatlas-compose stop worker`, then request a re-index | `worker-down` |
| Disk full | `fallocate` a file on the data volume past 80% | `data-disk-full` |
| Backup skipped | `systemctl disable --now codeatlas-backup.timer` until 26 hours after the last backup | `backup-missing` |

`jobs-waiting`, `jobs-failing`, and `certificate-expiring` are covered by the integration tests of
their metrics; prove their notification path with `aws cloudwatch set-alarm-state --state-value
ALARM`, then `OK`, for each, and record the emails.

## Recovery exercise (SC-008)

Follow "Recovery exercise" in the operations guide: build `drill` from the definition, release the
pilot's commit to it, restore the latest backup, check `verify-restore`, sign in on
`https://drill.<domain>`, and confirm your repositories, answers, and reviews. Record the start and
end times, then destroy `drill`. The target is under 2 hours, losing only changes made after the
backup.

## Load test (SC-011)

Needs the CloudWatch agent from User Story 2 for CPU and memory. Build `loadtest`, release the
pilot's commit to it, run the `load-test.yml` workflow against `https://loadtest.<domain>`, and
destroy `loadtest`. Write `docs/reports/load-test.md` from the workflow's artifact, with the steps
to rerun it. The 10-user level must meet 001 SC-007's targets; the report states the highest level
that does, or that all of them did.

## Evaluation (SC-012)

```bash
cd backend
uv run python -m evals.run_bench_eval --bench-commit <pinned commit> --out evals/out/bench   # Code Review Bench, Python and TypeScript
uv run python -m evals.run_qa_eval --set evals/qa_v1.jsonl --out evals/out
uv run python -m evals.run_review_eval --set evals/review_v2.jsonl --out evals/out
```

Each runner's options and required keys are in `backend/evals/README.md`. Check the providers'
model deprecation pages first, and run the benchmark before its published judges are retired
(research R16). Write `docs/reports/evaluation.md` from their
outputs, with the review status label of each set.

## Cost and availability checks (SC-001 to SC-003)

- After 14 days: the average of the `/readyz` check's `HealthCheckPercentageHealthy` over the
  period is at least 99%.
- After the first full month: Cost Explorer's total, excluding the domain, is at most $40.
- Daily: the provider's billing shows model spend below $15 on every day.

## Validation record

Filled in during final validation: dates, results, and any scenario not run, with the reason.
