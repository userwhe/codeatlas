import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError, errorMessage, unwrap } from "./client";
import { isActiveJob } from "./repositories";
import type { components } from "./schema";

export type Run = components["schemas"]["RunOut"];
export type RunSummary = components["schemas"]["RunSummaryOut"];
export type RunStatus = Run["status"];
export type QualityState = Run["quality_state"];
export type Claim = components["schemas"]["ClaimOut"];
export type Citation = components["schemas"]["CitationOut"];
export type Usage = components["schemas"]["UsageOut"];
type SubmitIn = components["schemas"]["SubmitIn"];

/** The longest question the API accepts, after trimming. */
export const MAX_QUESTION_CHARS = 2000;

const HISTORY_POLL_MS = 5000;

/** Today's question allowance of the workspace. Pass `enabled: false` while anonymous. */
export function useUsage({ enabled = true }: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: ["usage"],
    queryFn: () => unwrap(api.GET("/v1/usage")),
    enabled,
  });
}

/** One question and its answer, citations, or error. */
export function useRun(runId: string) {
  return useQuery({
    queryKey: ["analysis-runs", runId],
    queryFn: () =>
      unwrap(api.GET("/v1/analysis-runs/{run_id}", { params: { path: { run_id: runId } } })),
  });
}

/** The questions asked about a repository, newest first, one page per "Load more". */
export function useRunHistory(repositoryId: string) {
  return useInfiniteQuery({
    queryKey: ["analysis-runs", "list", repositoryId],
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/v1/analysis-runs", {
          // Reviews have their own pages and never appear in the question history.
          params: {
            query: { repository_id: repositoryId, kind: "repository_qa", cursor: pageParam },
          },
        }),
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor,
    // Keep statuses current while any listed question is still being answered.
    refetchInterval: (query) =>
      query.state.data?.pages.some((page) => page.items.some((item) => isActiveJob(item.status)))
        ? HISTORY_POLL_MS
        : false,
  });
}

/**
 * Submits a question. Each submission attempt passes a new idempotency key, so a replayed
 * request cannot queue the question twice. Usage is refreshed whether or not it succeeds.
 */
export function useAskQuestion() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ body, idempotencyKey }: { body: SubmitIn; idempotencyKey: string }) =>
      unwrap(
        api.POST("/v1/analysis-runs", {
          body,
          params: { header: { "Idempotency-Key": idempotencyKey } },
        }),
      ),
    onSuccess: (_, { body }) => {
      void queryClient.invalidateQueries({
        queryKey: ["analysis-runs", "list", body.repository_id],
      });
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["usage"] });
    },
  });
}

/**
 * A message for a failed submission. The messages for the workspace's daily limit and the
 * pilot-wide daily limit include the local reset time.
 */
export function questionErrorMessage(error: unknown): string {
  if (
    error instanceof ApiError &&
    (error.code === "daily_limit_reached" || error.code === "pilot_limit_reached")
  ) {
    const resetsAt = error.details.resets_at;
    if (typeof resetsAt === "string") {
      const local = new Date(resetsAt).toLocaleString();
      return `${error.message} New questions can be asked after ${local}.`;
    }
  }
  return errorMessage(error);
}

/** A short label for a question's state: its execution status, or the answer quality. */
export function runStateLabel(status: RunStatus, quality: QualityState): string {
  switch (status) {
    case "queued":
      return "Queued";
    case "running":
      return "Answering";
    case "retry_wait":
      return "Retrying";
    case "succeeded":
      return quality === "insufficient_evidence" ? "Insufficient evidence" : "Answered";
    case "failed":
      return "Failed";
    case "canceled":
      return "Canceled";
  }
}
