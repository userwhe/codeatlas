/**
 * End-to-end tests for repository Q&A (specs/001-repository-qa, quickstart scenarios 1, 2, 7,
 * 12, and 14).
 *
 * They run against a stack that is already running in fake mode (`CODEATLAS_ENV=development`,
 * `CODEATLAS_FAKE_EXTERNALS=1`): sign-in always succeeds as `octocat`, and the fake GitHub serves
 * the fixture repositories in backend/tests/fixtures/repos. See frontend/README.md.
 *
 * The tests can run again against the same database: a repository that is still connected from
 * an earlier run is opened instead of connected, and the disconnect test removes what it adds.
 */
import { expect, type Page, test } from "@playwright/test";

const SAMPLE_APP = "octo-org/sample-app";
const NO_CODE = "octo-org/no-code";
const QUESTION = "Where are repository permissions checked?";
const SYMBOL = "check_repository_access";
const SYMBOL_PATH = "app/auth/access.py";

// Indexing and answering run in the worker; on a cold stack the first job can take a while.
const INDEXING_TIMEOUT = 120_000;
const ANSWER_TIMEOUT = 60_000;

const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const REPOSITORY_URL = new RegExp(`/repositories/${UUID}$`);

/** Signs in through the fake GitHub, which approves `octocat` at once. */
async function signIn(page: Page) {
  await page.goto("/");
  await page.getByRole("link", { name: "Sign in with GitHub" }).click();
  await expect(page).toHaveURL(/\/repositories$/);
  await expect(page.getByRole("heading", { level: 1, name: "Repositories" })).toBeVisible();
  await expect(page.getByText("octocat", { exact: true })).toBeVisible();
}

/**
 * Connects a repository through the connect dialog and lands on its page. A repository that is
 * still connected from an earlier run is opened from the dialog instead.
 */
async function connectOrOpen(page: Page, fullName: string) {
  await page.goto("/repositories");
  await page.getByRole("button", { name: "Connect repository" }).click();
  const dialog = page.getByRole("dialog", { name: "Connect a repository" });
  await expect(dialog).toBeVisible();

  const row = dialog
    .getByRole("listitem")
    .filter({ has: page.getByText(fullName, { exact: true }) });
  await expect(row).toBeVisible();
  // The row is rendered from the loaded list, so whether it is connected is settled here.
  if (await row.getByText("Connected", { exact: true }).isVisible()) {
    await row.getByRole("link", { name: "Open" }).click();
  } else {
    await row.getByRole("radio").check();
    await dialog.getByRole("button", { name: "Connect", exact: true }).click();
  }

  await expect(page).toHaveURL(REPOSITORY_URL);
  await expect(page.getByRole("heading", { level: 1, name: fullName })).toBeVisible();
}

/** Waits until the open repository page shows the `Ready` state. */
async function expectReady(page: Page) {
  await expect(page.getByRole("main").getByText("Ready", { exact: true })).toBeVisible({
    timeout: INDEXING_TIMEOUT,
  });
  const indexedVersion = page.getByRole("region", { name: "Indexed version" });
  await expect(indexedVersion.getByText("Commit", { exact: true })).toBeVisible();
}

/** Checks, once the repository list has loaded, that it does not list `fullName`. */
async function expectNotListed(page: Page, fullName: string) {
  const main = page.getByRole("main");
  await expect(
    main.getByRole("list").or(main.getByText("No repositories are connected yet.")),
  ).toBeVisible();
  await expect(
    main.getByRole("link").filter({ has: page.getByText(fullName, { exact: true }) }),
  ).toHaveCount(0);
}

test.describe.serial("sample-app", () => {
  // The page of the connected sample-app, set by the first test.
  let repositoryUrl = "";

  test("signs in, connects sample-app, and indexes it", async ({ page }) => {
    await signIn(page);
    await connectOrOpen(page, SAMPLE_APP);
    await expectReady(page);
    repositoryUrl = page.url();
  });

  test("answers a question with citations that open in the file browser", async ({ page }) => {
    await signIn(page);
    await page.goto(repositoryUrl);
    await expectReady(page);

    await page.getByLabel("Ask a question").fill(QUESTION);
    await page.getByRole("button", { name: "Ask", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/answers/${UUID}$`));
    await expect(page.getByRole("heading", { level: 1, name: QUESTION })).toBeVisible();
    await expect(page.getByRole("heading", { level: 2, name: "Answer", exact: true })).toBeVisible({
      timeout: ANSWER_TIMEOUT,
    });
    await expect(page.getByText("Answered", { exact: true })).toBeVisible();

    // Expand the first citation's excerpt.
    const citation = page.getByRole("region", { name: "Citations" }).getByRole("listitem").first();
    const excerpt = citation.getByRole("table");
    await expect(excerpt).toBeHidden();
    await citation.getByRole("button", { name: "Show excerpt" }).click();
    await expect(citation.getByRole("button", { name: "Hide excerpt" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    await expect(excerpt).toBeVisible();
    const firstCitedLine = await excerpt.getByRole("row").first().locator("code").textContent();

    // The citation links to its file and line range at the answer's indexed commit.
    const open = citation.getByRole("link", { name: "Open in file browser" });
    const href = new URL((await open.getAttribute("href")) ?? "", "http://localhost");
    const path = href.searchParams.get("path") ?? "";
    const [start, end] = (href.searchParams.get("lines") ?? "").split("-").map(Number);
    expect(path).not.toBe("");
    expect(start).toBeGreaterThanOrEqual(1);
    expect(end).toBeGreaterThanOrEqual(start);

    await open.click();
    await expect(page).toHaveURL(new RegExp(`/snapshots/${UUID}/browse\\?`));
    const file = page.getByRole("region", { name: `File ${path}` });
    await expect(file.getByRole("heading", { name: path })).toBeVisible();
    await expect(file).toContainText(
      start === end ? `line ${start} highlighted` : `lines ${start}–${end} highlighted`,
    );

    // The cited lines are marked and scrolled into view, and they match the excerpt.
    const highlighted = /bg-yellow-100/;
    await expect(file.locator(`#L${start}`)).toBeInViewport();
    await expect(file.locator(`#L${start}`)).toHaveClass(highlighted);
    await expect(file.locator(`#L${end}`)).toHaveClass(highlighted);
    await expect(file.locator(`#L${start} code`)).toHaveText(firstCitedLine ?? "");
    if (start > 1) await expect(file.locator(`#L${start - 1}`)).not.toHaveClass(highlighted);
    const after = file.locator(`#L${end + 1}`);
    if ((await after.count()) > 0) await expect(after).not.toHaveClass(highlighted);
  });

  test("finds a symbol with the exact match first", async ({ page }) => {
    await signIn(page);
    await page.goto(repositoryUrl);
    await page.getByRole("link", { name: "Search", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/snapshots/${UUID}/search`));

    await page
      .getByRole("group", { name: "Search mode" })
      .getByText("Symbol", { exact: true })
      .click();
    await expect(page.getByRole("radio", { name: "Symbol" })).toBeChecked();
    await page.getByRole("searchbox", { name: "Search query" }).fill(SYMBOL);
    await page.getByRole("button", { name: "Search", exact: true }).click();

    const first = page
      .getByRole("region", { name: "Search results" })
      .getByRole("listitem")
      .first();
    await expect(first.getByText(SYMBOL_PATH, { exact: true })).toBeVisible();
    await expect(first.getByText(SYMBOL, { exact: true })).toBeVisible();
    await expect(first.getByText("Exact", { exact: true })).toBeVisible();

    // The result opens the declaration in the file browser.
    await first.getByRole("link").click();
    const file = page.getByRole("region", { name: `File ${SYMBOL_PATH}` });
    await expect(file).toContainText("highlighted");
  });
});

test("connects a repository, then disconnects it", async ({ page }) => {
  await signIn(page);
  await connectOrOpen(page, NO_CODE);
  await expectReady(page);
  const repositoryUrl = page.url();
  // no-code holds only Markdown and text, so no declarations are extracted.
  await expect(page.getByText("No code structure was extracted.")).toBeVisible();

  await page.getByRole("button", { name: "Disconnect repository" }).click();
  const dialog = page.getByRole("dialog", { name: `Disconnect ${NO_CODE}?` });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Disconnect", exact: true }).click();
  await expect(page).toHaveURL(/\/repositories$/);

  // Gone from the list, also after a reload, and its page no longer opens.
  await expectNotListed(page, NO_CODE);
  await page.reload();
  await expectNotListed(page, NO_CODE);
  await page.goto(repositoryUrl);
  await expect(
    page.getByText("This repository does not exist or is not connected to your workspace."),
  ).toBeVisible();
});
