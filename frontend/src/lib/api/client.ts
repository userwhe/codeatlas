import createClient, { type Middleware } from "openapi-fetch";

import type { paths } from "./schema";

/** An error response from the API: `{"error": {code, message, retryable, request_id, details}}`. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;
  readonly requestId: string | null;
  readonly details: Record<string, unknown>;

  constructor(status: number, body: unknown) {
    const error = (body as { error?: Partial<ApiErrorBody> } | null)?.error ?? {};
    super(error.message ?? `Request failed with status ${status}.`);
    this.name = "ApiError";
    this.status = status;
    this.code = error.code ?? "unknown_error";
    this.retryable = error.retryable ?? status >= 500;
    this.requestId = error.request_id ?? null;
    this.details = error.details ?? {};
  }
}

interface ApiErrorBody {
  code: string;
  message: string;
  retryable: boolean;
  request_id: string | null;
  details: Record<string, unknown>;
}

// A missing or expired session sends the browser to the sign-in page. The sign-in page itself
// calls /v1/me to detect an existing session, so it must not redirect to itself. This runs
// outside React, and a full navigation also drops every cached query of the old session.
const redirectOnUnauthenticated: Middleware = {
  onResponse({ response }) {
    if (
      response.status === 401 &&
      typeof window !== "undefined" &&
      window.location.pathname !== "/"
    ) {
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination -- intentional full navigation
      window.location.assign("/");
    }
  },
};

export const api = createClient<paths>({ baseUrl: "", credentials: "same-origin" });
api.use(redirectOnUnauthenticated);

/**
 * Whether a request failed because GitHub access to the repository was lost: the API then denies
 * every read of the repository's versions, files, search results, answers, and progress.
 */
export function isAccessLost(error: unknown): error is ApiError {
  return (
    error instanceof ApiError && error.status === 403 && error.code === "repository_access_lost"
  );
}

/** A message to show for a failed request: the API's message, or a generic network message. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "Could not reach CodeAtlas. Check your connection and try again.";
}

/**
 * Returns the response data, or throws an `ApiError` for an error response.
 * Use it in TanStack Query functions: `queryFn: () => unwrap(api.GET("/v1/me"))`.
 */
export async function unwrap<T>(
  request: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await request;
  if (!response.ok) {
    throw new ApiError(response.status, error);
  }
  return data as T;
}
