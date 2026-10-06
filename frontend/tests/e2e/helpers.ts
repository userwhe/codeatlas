/**
 * Helpers shared by the end-to-end specs. They drive the stack in fake mode, where sign-in always
 * succeeds as `octocat` and the fake GitHub serves the fixture repositories.
 */
import { expect, type Page } from "@playwright/test";

// Indexing and answering run in the worker; on a cold stack the first job can take a while.
export const INDEXING_TIMEOUT = 120_000;

export const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
export const REPOSITORY_URL = new RegExp(`/repositories/${UUID}$`);

/** Signs in through the fake GitHub, which approves `octocat` at once. */
export async function signIn(page: Page) {
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
export async function connectOrOpen(page: Page, fullName: string) {
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
export async function expectReady(page: Page) {
  await expect(page.getByRole("main").getByText("Ready", { exact: true })).toBeVisible({
    timeout: INDEXING_TIMEOUT,
  });
  const indexedVersion = page.getByRole("region", { name: "Indexed version" });
  await expect(indexedVersion.getByText("Commit", { exact: true })).toBeVisible();
}
