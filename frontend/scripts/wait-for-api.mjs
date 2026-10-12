// The web app's release gate, its pre-deploy command: `node scripts/wait-for-api.mjs`.
//
// It waits until the API at API_ORIGIN reports this deployment's commit (RAILWAY_GIT_COMMIT_SHA)
// in /version, which the API does only after its migration and readiness check passed, so a
// failed API stops the whole release (specs/004-pilot-deployment, research R7). It mirrors the
// backend's `python -m codeatlas.ops wait-for-api`, because this image has no Python.

const TIMEOUT_MS = 20 * 60 * 1_000;
const INTERVAL_MS = 5_000;
const REQUEST_TIMEOUT_MS = 5_000;

/** The commit in the API's /version, or null while it does not answer with one. */
async function reportedCommit(fetchImpl, origin) {
  try {
    const response = await fetchImpl(`${origin.replace(/\/+$/, "")}/version`, {
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    if (!response.ok) return null;
    const body = await response.json();
    return typeof body?.commit === "string" ? body.commit : null;
  } catch {
    return null;
  }
}

/**
 * Polls `<origin>/version` every `intervalMs` until it reports `commit`, and resolves false when
 * `timeoutMs` pass first. Another commit, an error status, or no answer means the API's new
 * version does not serve yet. Without a commit there is nothing to wait for.
 */
export async function waitForApi({
  origin,
  commit,
  timeoutMs = TIMEOUT_MS,
  intervalMs = INTERVAL_MS,
  fetchImpl = fetch,
  now = () => performance.now(),
  sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
}) {
  if (!commit) return true;
  const deadline = now() + timeoutMs;
  let reported = await reportedCommit(fetchImpl, origin);
  while (reported !== commit && now() < deadline) {
    await sleep(intervalMs);
    reported = await reportedCommit(fetchImpl, origin);
  }
  return reported === commit;
}

if (import.meta.main) {
  const origin = process.env.API_ORIGIN ?? "http://localhost:8000";
  // Unset outside a deployment from GitHub, where no API reports a commit to wait for.
  const commit = process.env.RAILWAY_GIT_COMMIT_SHA ?? "";
  const minutes = TIMEOUT_MS / 60_000;
  if (!commit) {
    console.log("No commit from the platform: the release gate passes without waiting.");
  } else {
    console.log(
      `Waiting up to ${minutes} minutes for the API at ${origin} to report commit ${commit}.`,
    );
    if (await waitForApi({ origin, commit })) {
      console.log(`The API reports commit ${commit}.`);
    } else {
      console.error(
        `error: the API at ${origin} did not report commit ${commit} within ${minutes} minutes.`,
      );
      process.exitCode = 1;
    }
  }
}
