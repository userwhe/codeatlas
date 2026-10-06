import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright end-to-end tests for CodeAtlas.
 *
 * The tests drive the running stack and do not start any servers. Before `npm run test:e2e`,
 * start the full stack (db, migrate, api, worker, web) in fake mode, with
 * `CODEATLAS_ENV=development` and `CODEATLAS_FAKE_EXTERNALS=1`, for example with
 * `docker compose up -d --build` from the repository root. The README in this directory has the
 * steps. The global setup stops the run early when the web app is unreachable or not in fake mode.
 *
 * `E2E_BASE_URL` points the tests at another web origin (default `http://localhost:3000`).
 */
export default defineConfig({
  testDir: "./tests/e2e",
  globalSetup: "./tests/e2e/global-setup.ts",
  // The tests share one database and one job worker, so they run one at a time.
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  // Indexing and answering run in the worker; the slowest test waits for both.
  timeout: 180_000,
  expect: { timeout: 15_000 },
  outputDir: "test-results",
  reporter: [["list"], ["html", { outputFolder: "playwright-report", open: "never" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    navigationTimeout: 60_000,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
