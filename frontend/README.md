# CodeAtlas web app

The Next.js (App Router) frontend for CodeAtlas. It talks to the FastAPI backend in `../backend`
through same-origin rewrites: `/v1/*` and `/auth/*` are proxied to `API_ORIGIN`
(default `http://localhost:8000`), so the session cookie never crosses origins.

## Requirements

- Node.js 24 (see `.nvmrc`)
- The API running on `API_ORIGIN` for anything beyond a static build

## Commands

```bash
npm ci                # install dependencies
npm run dev           # dev server on http://localhost:3000
npm run lint          # ESLint
npm run typecheck     # TypeScript, no emit
npm run build         # production build
npm run gen:api       # regenerate src/lib/api/schema.d.ts from http://localhost:8000/openapi.json
npm run test:e2e      # Playwright end-to-end tests; needs the stack in fake mode (see below)
```

`API_ORIGIN` is read when the dev server starts or when `npm run build` runs, so set it before
either command when the API is not on `http://localhost:8000`.

Run `npm run gen:api` with the API running whenever backend routes or models change, and commit
the regenerated `src/lib/api/schema.d.ts`.

## End-to-end tests

The Playwright tests in `tests/e2e/` drive the whole stack through Chromium: sign in, connect
`octo-org/sample-app` and wait for indexing, ask a question and open a citation in the file browser,
run a symbol search, and connect and disconnect `octo-org/no-code`. They also send signed webhook
deliveries straight to the API: a push, which shows up as a push-started run, and the repository's
removal from the App installation, which hides its content until a re-index restores access. A
review test connects `octo-org/review-app`, reviews its pull request #1, checks the risk, its
citation, the checklist, the candidate tests, and the Markdown copy, and uses one of the workspace's
10 daily reviews per run. They expect the stack to be running already in fake mode, which serves the
fixture repositories in `backend/tests/fixtures/repos` and uses fake model providers.
`playwright.config.ts` does not start any servers, and the run stops early if the web app is
unreachable or not in fake mode.

1. In the repository root, create `.env` from `.env.example` and set `CODEATLAS_ENV=development`,
   `CODEATLAS_FAKE_EXTERNALS=1`, `GITHUB_WEBHOOK_SECRET` to any random string (for example from
   `openssl rand -hex 32`), and `TOKEN_ENCRYPTION_KEY` to a new key printed by:

   ```bash
   python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
   ```

2. Start the stack from the repository root. This runs the migrations, then the API, the worker,
   and the web app on http://localhost:3000:

   ```bash
   docker compose up -d --build
   ```

3. In this directory, install Chromium once, then run the tests:

   ```bash
   npx playwright install chromium
   GITHUB_WEBHOOK_SECRET=<the value from .env> npm run test:e2e
   ```

   The tests post webhook deliveries to the API at `E2E_API_URL` (default
   `http://localhost:8000`).

4. Stop the app containers when done: `docker compose stop api worker web` (from the repository
   root).

Set `E2E_BASE_URL` to test a web app on another origin. On failure, `npx playwright show-report`
opens the HTML report in `playwright-report/`, with a trace and a screenshot of each failed test;
the raw files are in `test-results/`.

The tests can run again against the same database. `sample-app` stays connected and is reused,
and the disconnect test removes `no-code` again. Each run asks one question, which counts toward
the daily allowance (`DAILY_QUESTION_LIMIT`, 20 by default); raise it in `.env` for many runs in
one day.

If `.env` is set up for a real GitHub App, keep it as it is and pass the fake-mode settings in a
Compose override file instead, since `environment` entries take precedence over `.env`. Keep the
file outside the repository, list the same variables under `environment` for the `migrate`, `api`,
and `worker` services, and start the stack with
`docker compose -f docker-compose.yml -f <override file> up -d --build`. Setting `DATABASE_URL`
there to another database on the same server (for example `codeatlas_e2e`, created with
`docker compose exec db createdb -U codeatlas codeatlas_e2e`) keeps the test data apart from your
development data.

## Layout

- `src/app/`: routes, the root layout, and the TanStack Query provider
- `src/components/`: shared components such as `JobProgress`
- `src/lib/api/`: the typed API client (`client.ts`) and the generated schema (`schema.d.ts`)
