// The web app's release gate (T017, research R7), run with `node --test scripts/`. `fetch` is
// stubbed, and a fake clock moves only when the gate sleeps.
import assert from "node:assert/strict";
import { test } from "node:test";

import { waitForApi } from "./wait-for-api.mjs";

const ORIGIN = "http://api.railway.internal:8000";
const COMMIT = "3f9c2e1d4b5a69788c7d0e1f2a3b4c5d6e7f8091";
const OLDER_COMMIT = "9a8b7c6d5e4f30211f2e3d4c5b6a79880f1e2d3c";

/** A clock that only `sleep` moves. */
class FakeTime {
  current = 0;
  sleeps = [];
  now = () => this.current;
  sleep = async (ms) => {
    this.sleeps.push(ms);
    this.current += ms;
  };
}

function version(commit) {
  return Response.json({ commit });
}

/**
 * A `fetch` giving these answers in turn, and the last one again after that, with the requests it
 * received. An answer is a response, or an error the request rejects with.
 */
function replies(...answers) {
  const requests = [];
  async function fetchImpl(url, init) {
    requests.push({ url: String(url), signal: init?.signal });
    const answer = answers[Math.min(requests.length, answers.length) - 1];
    if (answer instanceof Error) throw answer;
    return answer.clone();
  }
  return { fetchImpl, requests };
}

function wait({ fetchImpl }, time, options = {}) {
  return waitForApi({
    origin: ORIGIN,
    commit: COMMIT,
    timeoutMs: 60_000,
    intervalMs: 5_000,
    fetchImpl,
    now: time.now,
    sleep: time.sleep,
    ...options,
  });
}

test("passes as soon as the API reports its commit", async () => {
  const time = new FakeTime();
  const api = replies(
    Response.json({ error: { code: "not_ready" } }, { status: 503 }),
    new TypeError("fetch failed"),
    version(OLDER_COMMIT),
    version(COMMIT),
    version(OLDER_COMMIT),
  );

  assert.equal(await wait(api, time), true);

  assert.deepEqual(
    api.requests.map((request) => request.url),
    Array(4).fill(`${ORIGIN}/version`),
  );
  // Each request gives up on its own, so one that hangs cannot hold the gate past its deadline.
  assert.ok(api.requests.every((request) => request.signal instanceof AbortSignal));
  assert.deepEqual(time.sleeps, [5_000, 5_000, 5_000]);
});

test("keeps waiting for another commit until the deadline", async () => {
  const time = new FakeTime();
  const api = replies(version(OLDER_COMMIT));

  assert.equal(await wait(api, time), false);

  // One request at the start and one after each of the 12 sleeps of 5 seconds.
  assert.equal(api.requests.length, 13);
  assert.equal(time.current, 60_000);
});

for (const [name, answer] of [
  ["an error status", new Response(null, { status: 503 })],
  ["a body without a commit", Response.json({ status: "ok" })],
  ["a body that is not JSON", new Response("not json")],
  ["a refused connection", new TypeError("fetch failed")],
  ["a request that times out", new DOMException("The operation timed out.", "TimeoutError")],
]) {
  test(`keeps waiting on ${name}`, async () => {
    const time = new FakeTime();
    const api = replies(answer);

    assert.equal(await wait(api, time, { timeoutMs: 20_000 }), false);

    assert.equal(api.requests.length, 5);
  });
}

test("passes at once without a commit", async () => {
  const time = new FakeTime();
  const api = replies(version(COMMIT));

  assert.equal(await wait(api, time, { commit: "" }), true);

  assert.deepEqual(api.requests, []);
  assert.deepEqual(time.sleeps, []);
});

test("defaults to 20 minutes, every 5 seconds", async () => {
  const time = new FakeTime();
  const { fetchImpl } = replies(new TypeError("fetch failed"));

  const passed = await waitForApi({
    origin: ORIGIN,
    commit: COMMIT,
    fetchImpl,
    now: time.now,
    sleep: time.sleep,
  });

  assert.equal(passed, false);
  assert.equal(time.current, 20 * 60 * 1_000);
  assert.deepEqual(new Set(time.sleeps), new Set([5_000]));
});
