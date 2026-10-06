/**
 * End-to-end tests for automatic re-indexing and access revocation (specs/002-push-reindexing,
 * research R10).
 *
 * The tests send signed GitHub webhook deliveries straight to the API, signed with
 * `GITHUB_WEBHOOK_SECRET`. Fake GitHub state lives in each process, so they drive only what the
 * API applies on its own: a push is recorded and starts a `push` run, which reuses the indexed
 * version because the worker's fake `main` has not moved; and removing the repository from the
 * installation marks it lost at once. Commit-level behavior is covered by the backend
 * integration tests.
 *
 * Only `sample-app` is removed from the installation, so other specs' repositories under the same
 * installation stay readable. Each test leaves `sample-app` readable again.
 */
import { createHash, createHmac, randomUUID } from "node:crypto";

import { type APIRequestContext, expect, type Page, test } from "@playwright/test";

import { connectOrOpen, expectReady, INDEXING_TIMEOUT, signIn } from "./helpers";

const SAMPLE_APP = "octo-org/sample-app";
const SAMPLE_APP_ID = 2001;
const OCTO_ORG_INSTALLATION_ID = 5001;
const OCTOCAT_ID = 1001;

const API_URL = process.env.E2E_API_URL ?? "http://localhost:8000";
const WEBHOOK_SECRET = process.env.GITHUB_WEBHOOK_SECRET ?? "";

/** The fake GitHub's SHA of a fixture commit (`commit_sha` in backend/src/codeatlas/github/fake.py). */
function fakeCommitSha(commitName: string) {
  return createHash("sha1").update(`${SAMPLE_APP_ID}:${commitName}`).digest("hex");
}

/** Sends one signed delivery to the API's webhook endpoint and returns its outcome. */
async function sendDelivery(request: APIRequestContext, event: string, payload: object) {
  const body = JSON.stringify(payload);
  const signature = createHmac("sha256", WEBHOOK_SECRET).update(body).digest("hex");
  const response = await request.post(`${API_URL}/webhooks/github`, {
    data: body,
    headers: {
      "Content-Type": "application/json",
      "X-GitHub-Event": event,
      "X-GitHub-Delivery": randomUUID(),
      "X-Hub-Signature-256": `sha256=${signature}`,
    },
  });
  expect(response.status(), await response.text()).toBe(202);
  return ((await response.json()) as { outcome: string }).outcome;
}

const repository = { id: SAMPLE_APP_ID, full_name: SAMPLE_APP, private: false };

/** Opens sample-app, re-indexing it first if an earlier, interrupted run left it lost. */
async function openReadySampleApp(page: Page) {
  await signIn(page);
  await connectOrOpen(page, SAMPLE_APP);
  const main = page.getByRole("main");
  if (await main.getByText("Access lost", { exact: true }).first().isVisible()) {
    await main.getByRole("button", { name: "Re-index" }).click();
  }
  await expectReady(page);
}

test("a push delivery shows the latest push and a push-started run", async ({ page, request }) => {
  await openReadySampleApp(page);
  const head = fakeCommitSha("initial");

  const outcome = await sendDelivery(request, "push", {
    ref: "refs/heads/main",
    before: "0".repeat(40),
    after: head,
    deleted: false,
    repository: { ...repository, default_branch: "main" },
    installation: { id: OCTO_ORG_INSTALLATION_ID },
    sender: { id: OCTOCAT_ID, login: "octocat" },
  });
  expect(outcome).toBe("processed");

  await page.reload();
  const updates = page.getByRole("region", { name: "Automatic updates" });
  await expect(updates.getByText(head.slice(0, 7), { exact: true })).toBeVisible();
  // The push run finds the commit already indexed and reuses that version.
  await expect(page.getByText(/Latest run:\s*After a push\s*·\s*Indexed/)).toBeVisible({
    timeout: INDEXING_TIMEOUT,
  });
  await expectReady(page);
});

test("losing access hides the repository, and re-indexing restores it", async ({
  page,
  request,
}) => {
  await openReadySampleApp(page);
  const repositoryUrl = page.url();
  const browseUrl = await page.getByRole("link", { name: "Browse files" }).getAttribute("href");
  expect(browseUrl).not.toBeNull();

  const outcome = await sendDelivery(request, "installation_repositories", {
    action: "removed",
    installation: { id: OCTO_ORG_INSTALLATION_ID },
    repository_selection: "selected",
    repositories_added: [],
    repositories_removed: [repository],
    sender: { id: OCTOCAT_ID, login: "octocat" },
  });
  expect(outcome).toBe("processed");

  // The repository page explains what happened, with the date its data would be deleted.
  await page.reload();
  const main = page.getByRole("main");
  await expect(main.getByText("Access lost", { exact: true }).first()).toBeVisible();
  const access = page.getByRole("region", { name: "GitHub access" });
  await expect(access.getByText("Data deleted after", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Browse files" })).toHaveCount(0);

  // The file browser shows the access-lost notice instead of the files.
  await page.goto(browseUrl ?? "");
  await expect(
    page.getByText("CodeAtlas can no longer read its repository on GitHub", { exact: false }),
  ).toBeVisible();

  // Re-indexing checks access again; the worker's fake still covers the repository.
  await page.goto(repositoryUrl);
  await page.getByRole("button", { name: "Re-index" }).click();
  await expectReady(page);
  await expect(main.getByText("Access lost", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Could not load progress", { exact: false })).toHaveCount(0);
});
