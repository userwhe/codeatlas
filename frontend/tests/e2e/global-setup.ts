import type { FullConfig } from "@playwright/test";

const UNKNOWN_ID = "00000000-0000-0000-0000-000000000000";

// Every page the tests visit. Requesting each once compiles it on the development server, so the
// first test does not wait for compilation in the middle of a flow.
const PAGES = [
  "/",
  "/repositories",
  `/repositories/${UNKNOWN_ID}`,
  `/answers/${UNKNOWN_ID}`,
  `/snapshots/${UNKNOWN_ID}/browse`,
  `/snapshots/${UNKNOWN_ID}/search`,
];

const HOW_TO_START = "Start the stack in fake mode first; see frontend/README.md.";

/**
 * Checks that the stack is running in fake mode before any test runs, then warms up the pages.
 * Fake mode matters: the tests sign in and connect repositories, which must never reach GitHub.
 */
export default async function globalSetup(config: FullConfig) {
  const baseURL = config.projects[0]?.use.baseURL ?? "http://localhost:3000";

  // The webhook tests sign deliveries with the stack's secret and send them to the API directly.
  if (!process.env.GITHUB_WEBHOOK_SECRET) {
    throw new Error(
      "Set GITHUB_WEBHOOK_SECRET to the value the stack uses before running the tests. " +
        HOW_TO_START,
    );
  }
  const apiURL = process.env.E2E_API_URL ?? "http://localhost:8000";
  try {
    const health = await fetch(new URL("/healthz", apiURL));
    if (!health.ok) throw new Error(`GET /healthz returned ${health.status}`);
  } catch (error) {
    throw new Error(`The CodeAtlas API is not reachable at ${apiURL}. ${HOW_TO_START}`, {
      cause: error,
    });
  }

  let login: Response;
  try {
    login = await fetch(new URL("/auth/github/login", baseURL), { redirect: "manual" });
  } catch (error) {
    throw new Error(`CodeAtlas is not reachable at ${baseURL}. ${HOW_TO_START}`, {
      cause: error,
    });
  }
  // In fake mode, sign-in redirects straight to the callback instead of to GitHub.
  const location = login.headers.get("location") ?? "";
  if (login.status !== 302 || !location.startsWith("/auth/github/callback?")) {
    throw new Error(
      `CodeAtlas at ${baseURL} is not in fake mode (sign-in answered ${login.status}` +
        `${location ? ` with a redirect to ${new URL(location, baseURL).origin}` : ""}). ` +
        HOW_TO_START,
    );
  }

  for (const path of PAGES) {
    const response = await fetch(new URL(path, baseURL));
    if (!response.ok) {
      throw new Error(`GET ${path} returned ${response.status} while warming up the web app.`);
    }
  }
}
