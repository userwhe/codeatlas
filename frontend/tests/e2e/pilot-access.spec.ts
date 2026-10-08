/**
 * End-to-end tests for the pilot's access messages (specs/004-pilot-deployment, user story 1).
 *
 * They run against a stack that is already running in fake mode (`CODEATLAS_ENV=development`,
 * `CODEATLAS_FAKE_EXTERNALS=1`), where the access list does not apply and the pilot-wide limits
 * are far from reached. The refusals are therefore produced in the browser: the page for a denied
 * sign-in is opened by its URL, and the pilot-wide limit is a mocked API response. The backend
 * integration tests cover the server side. See frontend/README.md.
 *
 * The tests can run again against the same database: a sample-app connection left by an earlier
 * run is opened instead of connected, and the mocked question never reaches the API, so it uses
 * none of the daily allowance.
 */
import { expect, test } from "@playwright/test";

import { connectOrOpen, expectReady, signIn } from "./helpers";

const SAMPLE_APP = "octo-org/sample-app";
const QUESTION = "Where are repository permissions checked?";

const PILOT_LIMIT_MESSAGE =
  "CodeAtlas has reached today's limit of 30 questions for all pilot users.";
const RESETS_AT = "2030-01-01T00:00:00Z";
const PILOT_LIMIT_BODY = {
  error: {
    code: "pilot_limit_reached",
    message: PILOT_LIMIT_MESSAGE,
    retryable: false,
    request_id: null,
    details: { resets_at: RESETS_AT, limit: 30, allowance: "questions" },
  },
};

test("explains a denied sign-in without offering to sign in again", async ({ page }) => {
  await page.goto("/?error=not_invited");
  await expect(
    page.getByRole("heading", { level: 1, name: "CodeAtlas is in a private pilot" }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Your GitHub account is not on the pilot's access list. CodeAtlas did not keep any data from this sign-in.",
    ),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign in with GitHub" })).toHaveCount(0);
});

test("shows the pilot-wide question limit with its local reset time", async ({ page }) => {
  await signIn(page);
  await connectOrOpen(page, SAMPLE_APP);
  await expectReady(page);

  // Refuse the submission as the API does once all pilot users have used today's questions.
  await page.route("**/v1/analysis-runs", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await route.fulfill({ status: 429, json: PILOT_LIMIT_BODY });
  });

  await page.getByLabel("Ask a question").fill(QUESTION);
  await page.getByRole("button", { name: "Ask", exact: true }).click();

  // The reset time is shown in the browser's locale and time zone.
  const local = await page.evaluate((resetsAt) => new Date(resetsAt).toLocaleString(), RESETS_AT);
  const alert = page.getByRole("main").getByRole("alert").filter({ hasText: PILOT_LIMIT_MESSAGE });
  await expect(alert).toHaveText(
    `${PILOT_LIMIT_MESSAGE} New questions can be asked after ${local}.`,
  );
});

test("shows the running release in the footer", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("contentinfo")).toHaveText("Release development");
});
