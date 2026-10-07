/**
 * End-to-end test for pull request reviews (specs/003-pr-review).
 *
 * It runs against a stack that is already running in fake mode (`CODEATLAS_ENV=development`,
 * `CODEATLAS_FAKE_EXTERNALS=1`): the fake GitHub serves `octo-org/review-app` with the pull
 * requests in backend/tests/fixtures/pull-requests, and the fake review model answers in its `ok`
 * mode. Pull request #1, "Simplify write checks", drops the role condition from `can_write` in
 * `app/auth/permissions.py`, so its review has a high security risk that cites the removed lines.
 * See frontend/README.md.
 *
 * The test can run again against the same database: a `review-app` connection left by an earlier
 * run, whose #1 is no longer unreviewed, is disconnected and connected again. Each run requests
 * one review, which counts toward the daily allowance (`DAILY_REVIEW_LIMIT`, 10 by default).
 */
import { expect, type Locator, type Page, test } from "@playwright/test";

import { connectOrOpen, expectReady, signIn, UUID } from "./helpers";

const REVIEW_APP = "octo-org/review-app";
const PULL_REQUEST_TITLE = "Simplify write checks";
const CHANGED_PATH = "app/auth/permissions.py";
const CANDIDATE_TEST = "tests/test_permissions.py";
// Part of the condition #1 removes, shown in the excerpt of the merge-base side.
const REMOVED_CODE = "user.role in WRITE_ROLES";
const UNKNOWN_ID = "00000000-0000-0000-0000-000000000000";

const REVIEW_URL = new RegExp(`/reviews/${UUID}$`);
// The review page's requests for the run itself, not for its Markdown copy or freshness.
const RUN_REQUEST = new RegExp(`/v1/analysis-runs/${UUID}$`);
const FINISHED_STATUSES = ["succeeded", "failed", "canceled"];
// Reviewing runs in the worker; on a cold stack the job can wait for a while.
const REVIEW_TIMEOUT = 60_000;

/** Pull request #1's row in the open repository page's "Pull requests" section. */
function firstPullRequest(page: Page) {
  return page
    .getByRole("region", { name: "Pull requests" })
    .getByRole("listitem")
    .filter({ has: page.getByText("#1", { exact: true }) });
}

/** The review state shown in a pull request row. */
function reviewState(row: Locator) {
  return row.getByText(/^(Not reviewed|Queued|Reviewing|Current|Outdated|Failed)$/);
}

/** The "Overall risk" line of the open review. */
function overallRisk(page: Page) {
  return page.getByRole("region", { name: "Overview" }).locator("p", { hasText: "Overall risk" });
}

/**
 * Connects review-app and waits until it is ready, with #1 not reviewed yet. A connection left by
 * an earlier run already has a review of #1, so it is disconnected first, which removes its
 * reviews.
 */
async function openUnreviewedReviewApp(page: Page) {
  await connectOrOpen(page, REVIEW_APP);
  await expectReady(page);

  const state = reviewState(firstPullRequest(page));
  await expect(state).toBeVisible();
  if ((await state.textContent()) === "Not reviewed") return;

  await page.getByRole("button", { name: "Disconnect repository" }).click();
  const dialog = page.getByRole("dialog", { name: `Disconnect ${REVIEW_APP}?` });
  await dialog.getByRole("button", { name: "Disconnect", exact: true }).click();
  await expect(page).toHaveURL(/\/repositories$/);
  await connectOrOpen(page, REVIEW_APP);
  await expectReady(page);
}

/**
 * In fake mode a review takes a fraction of a second, so the review page replaces its progress
 * with the review as soon as the job's last poll reports it finished. Until the returned function
 * is called, this holds back each response for the run that shows it finished, so the stages stay
 * on screen. Responses are passed on unchanged, only later.
 */
async function holdFinishedRun(page: Page) {
  let release = () => {};
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  const matches = (url: URL) => RUN_REQUEST.test(url.pathname);
  await page.route(matches, async (route) => {
    const response = await route.fetch();
    const { status } = (await response.json()) as { status?: string };
    if (status !== undefined && FINISHED_STATUSES.includes(status)) await released;
    // The page may have dropped the request in the meantime.
    await route.fulfill({ response }).catch(() => {});
  });
  return async () => {
    release();
    await page.unroute(matches);
  };
}

test("reviews a pull request, copies the review, and shows it as current", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await signIn(page);
  // An unknown review is reported as such. Opening one first also has a development server
  // compile the review page before the review below is timed.
  await page.goto(`/reviews/${UNKNOWN_ID}`);
  await expect(
    page.getByText("This review does not exist or is no longer available."),
  ).toBeVisible();

  await openUnreviewedReviewApp(page);
  const repositoryUrl = page.url();

  // The list shows #1, read from the fake GitHub, with no review yet.
  const row = firstPullRequest(page);
  await expect(row.getByText(PULL_REQUEST_TITLE, { exact: true })).toBeVisible();
  await expect(reviewState(row)).toHaveText("Not reviewed");
  const headSha = (await row.locator("code[title]").getAttribute("title")) ?? "";
  expect(headSha).toMatch(/^[0-9a-f]{40}$/);
  const shortHead = headSha.slice(0, 7);

  const releaseRun = await holdFinishedRun(page);
  await row.getByRole("button", { name: "Review", exact: true }).click();
  await expect(page).toHaveURL(REVIEW_URL);
  const reviewUrl = page.url();
  await expect(
    page.getByRole("heading", { level: 1, name: `#1 ${PULL_REQUEST_TITLE}` }),
  ).toBeVisible();

  // The job's stages appear while the review runs.
  const progress = page.getByRole("region", { name: "Job progress" });
  for (const stage of ["Checking access", "Resolving commits", "Fetching source"]) {
    await expect(progress.getByText(stage, { exact: true })).toBeVisible({
      timeout: REVIEW_TIMEOUT,
    });
  }
  await releaseRun();

  // Then the review, which starts with the overall risk.
  await expect(overallRisk(page)).toHaveText(/^Overall risk\s*High$/, { timeout: REVIEW_TIMEOUT });
  await expect(progress).toHaveCount(0);
  await expect(page.getByRole("main").getByText("Reviewed", { exact: true }).first()).toBeVisible();
  const head = await page.locator("dt:text-is('Head') + dd").textContent();
  const mergeBase = await page.locator("dt:text-is('Merge base') + dd").textContent();
  expect(head).toBe(shortHead);
  expect(mergeBase).toMatch(/^[0-9a-f]{7}$/);

  // A risk whose citation shows the removed lines, labeled with the merge-base side.
  const risk = page
    .getByRole("region", { name: "Risks" })
    .getByRole("listitem")
    .filter({ has: page.getByRole("heading", { name: "The change may weaken a check" }) });
  await expect(risk.getByText("Security", { exact: true })).toBeVisible();
  await expect(risk.getByText(CHANGED_PATH, { exact: true })).toBeVisible();
  const citation = risk.getByRole("list", { name: /^Citations for R\d+$/ }).getByRole("listitem");
  await expect(citation).toHaveCount(1);
  const toggle = citation.getByRole("button", { name: "Show excerpt" });
  const excerpt = page.locator(`[id="${await toggle.getAttribute("aria-controls")}"]`);
  await expect(excerpt).toBeHidden();
  await toggle.click();
  await expect(citation.getByRole("button", { name: "Hide excerpt" })).toHaveAttribute(
    "aria-expanded",
    "true",
  );
  await expect(excerpt).toBeVisible();
  await expect(
    excerpt.getByText(`Before · merge base ${mergeBase}`, { exact: true }),
  ).toBeVisible();
  await expect(excerpt.getByRole("table")).toContainText(REMOVED_CODE);

  // The checklist names the changed file.
  const checklist = page.getByRole("region", { name: "Checklist" });
  await expect(checklist.getByRole("listitem")).not.toHaveCount(0);
  await expect(checklist.getByText(CHANGED_PATH, { exact: true }).first()).toBeVisible();

  // Candidate tests are labeled as not run by CodeAtlas.
  const tests = page.getByRole("region", { name: "Tests" });
  await expect(
    tests.getByRole("heading", { name: "Candidate tests (not run by CodeAtlas)" }),
  ).toBeVisible();
  const candidate = tests.getByRole("listitem").filter({
    has: page.getByText(CANDIDATE_TEST, { exact: true }),
  });
  await expect(
    candidate.getByText("Candidate test (not run by CodeAtlas)", { exact: true }),
  ).toBeVisible();

  // Coverage lists the changed file as reviewed.
  const coverage = page.getByRole("region", { name: "Coverage" });
  await expect(coverage.getByText(/^Reviewed 1 changed file of 1 ·/)).toBeVisible();
  const coverageRow = coverage.getByRole("row").filter({ hasText: CHANGED_PATH });
  await expect(coverageRow.getByRole("cell").last()).toHaveText("Reviewed");

  // The Markdown copy starts with the review heading.
  const overview = page.getByRole("region", { name: "Overview" });
  await overview.getByRole("button", { name: "Copy as Markdown" }).click();
  await expect(overview.getByRole("status")).toHaveText("Copied");
  const markdown = await page.evaluate(() => navigator.clipboard.readText());
  expect(markdown.split("\n")[0]).toBe(`## CodeAtlas review of #1 at ${shortHead}`);
  expect(markdown).toContain("**Overall risk: high**");

  // Back on the repository page, #1 has a current review, which opens the same review.
  await page.getByRole("link", { name: `← ${REVIEW_APP}` }).click();
  await expect(page).toHaveURL(repositoryUrl);
  const reviewedRow = firstPullRequest(page);
  await expect(reviewState(reviewedRow)).toHaveText("Current");
  await reviewedRow.getByRole("link", { name: "View review" }).click();
  await expect(page).toHaveURL(reviewUrl);
  await expect(
    page.getByRole("heading", { level: 1, name: `#1 ${PULL_REQUEST_TITLE}` }),
  ).toBeVisible();
  await expect(overallRisk(page)).toHaveText(/^Overall risk\s*High$/);
});
