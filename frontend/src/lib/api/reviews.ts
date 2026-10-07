import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError, errorMessage, unwrap } from "./client";
import { isActiveJob } from "./repositories";
import type { components } from "./schema";

export type PullRequest = components["schemas"]["PullRequestOut"];
export type PullRequestReview = components["schemas"]["PullRequestReviewOut"];
export type ReviewState = PullRequestReview["state"];
export type ReviewRun = components["schemas"]["ReviewRunOut"];
export type ReviewStatus = ReviewRun["status"];
export type ReviewQualityState = ReviewRun["quality_state"];
export type Review = components["schemas"]["ReviewOut"];
export type ReviewCoverage = components["schemas"]["ReviewCoverageOut"];
export type CoverageFile = components["schemas"]["CoverageFileOut"];
export type SummaryArea = components["schemas"]["SummaryAreaOut"];
export type SummaryPoint = components["schemas"]["SummaryPointOut"];
export type Risk = components["schemas"]["ReviewRiskOut"];
export type RiskLevel = components["schemas"]["OverallRiskOut"]["level"];
export type ReviewCitation = components["schemas"]["ReviewCitationOut"];
export type FileChange = SummaryPoint["change"];
export type ChecklistItem = components["schemas"]["ChecklistItemOut"];
export type ReviewTests = components["schemas"]["ReviewTestsOut"];
export type CandidateTest = components["schemas"]["CandidateTestOut"];
export type NewTestCase = components["schemas"]["NewTestCaseOut"];

const PULL_REQUEST_POLL_MS = 10_000;
const REVIEW_POLL_MS = 3000;

/** Whether a pull request's review is waiting or running. */
function inProgress(review: PullRequestReview | null) {
  return review?.state === "queued" || review?.state === "running";
}

/**
 * The repository's open pull requests, read live from GitHub, most recently updated first; one
 * page of up to 30 per `fetchNextPage`. While a listed review is queued or running, the list is
 * refreshed so that its state stays current.
 */
export function usePullRequests(repositoryId: string) {
  return useInfiniteQuery({
    queryKey: ["pull-requests", repositoryId],
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/v1/repositories/{repository_id}/pull-requests", {
          params: { path: { repository_id: repositoryId }, query: { cursor: pageParam } },
        }),
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor,
    refetchInterval: (query) =>
      query.state.data?.pages.some((page) => page.items.some((item) => inProgress(item.review)))
        ? PULL_REQUEST_POLL_MS
        : false,
  });
}

/** One review: its pull request, commits, and, once it succeeds, the review and coverage. */
export function useReview(runId: string) {
  return useQuery({
    // The same key as `useRun`: both read the same run.
    queryKey: ["analysis-runs", runId],
    queryFn: () =>
      unwrap(api.GET("/v1/analysis-runs/{run_id}", { params: { path: { run_id: runId } } })),
    // Follow the run until it finishes: the merge base and the job appear while it is active.
    refetchInterval: (query) => {
      if (query.state.error instanceof ApiError && query.state.error.status < 500) return false;
      const run = query.state.data;
      return run && isActiveJob(run.status) ? REVIEW_POLL_MS : false;
    },
  });
}

/**
 * The Markdown copy of a succeeded review, with citations linking to GitHub. Enable it only once
 * the review has succeeded (the API answers 409 before), so that "Copy as Markdown" usually has the
 * text at hand and can write it to the clipboard within the click.
 */
export function useReviewMarkdown(runId: string, enabled = true) {
  return useQuery({
    // Under the run's key, so that refreshing the run refreshes its copy too.
    queryKey: ["analysis-runs", runId, "markdown"],
    queryFn: async () => {
      const body = await unwrap(
        api.GET("/v1/analysis-runs/{run_id}/markdown", { params: { path: { run_id: runId } } }),
      );
      return body.markdown;
    },
    enabled,
    // A finished review does not change.
    staleTime: Infinity,
  });
}

/**
 * Requests a review of a pull request at its current head. Each submission attempt passes a new
 * idempotency key, so a replayed request cannot queue the review twice. Usage is refreshed whether
 * or not it succeeds.
 */
export function useRequestReview() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      repositoryId,
      pullRequestNumber,
      idempotencyKey,
    }: {
      repositoryId: string;
      pullRequestNumber: number;
      idempotencyKey: string;
    }) =>
      unwrap(
        api.POST("/v1/analysis-runs", {
          body: {
            repository_id: repositoryId,
            kind: "pull_request_review",
            target: { pull_request_number: pullRequestNumber },
          },
          params: { header: { "Idempotency-Key": idempotencyKey } },
        }),
      ),
    onSuccess: (_, { repositoryId }) => {
      void queryClient.invalidateQueries({ queryKey: ["pull-requests", repositoryId] });
    },
    onError: (error, { repositoryId }) => {
      // The repository was paused or lost access after the page loaded: reload it to show why.
      if (
        error instanceof ApiError &&
        (error.code === "external_processing_not_accepted" ||
          error.code === "repository_access_lost")
      ) {
        void queryClient.invalidateQueries({ queryKey: ["repositories", repositoryId] });
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["usage"] });
    },
  });
}

/** A message for a failed pull request list or review request. */
export function reviewErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return errorMessage(error);
  switch (error.code) {
    case "pull_requests_permission_missing":
      return "The CodeAtlas GitHub App needs the Pull requests (read-only) permission on this repository. Ask the account owner to approve it in the app's installation settings.";
    case "pull_request_not_open":
      return "This pull request is closed or merged. Only open pull requests can be reviewed.";
    case "pull_request_not_found":
      return "GitHub has no such pull request in this repository.";
    case "daily_limit_reached": {
      const resetsAt = error.details.resets_at;
      const questions = error.details.allowance === "questions";
      if (typeof resetsAt === "string") {
        const local = new Date(resetsAt).toLocaleString();
        const next = questions ? "New questions can be asked" : "New reviews can be requested";
        return `${error.message} ${next} after ${local}.`;
      }
      return error.message;
    }
    case "repository_rejected":
      return "This repository was rejected because it exceeds a size limit, so its pull requests cannot be reviewed.";
    case "external_processing_not_accepted":
      return "This private repository is paused until the external processing disclosure is accepted. Re-index it and accept the disclosure, then request the review again.";
    case "github_sign_in_required":
      return "Your GitHub authorization has expired. Sign out, then sign in with GitHub again.";
    case "github_unavailable":
      return "GitHub could not be reached. Try again shortly.";
    default:
      return error.message;
  }
}

const REVIEW_STATE_LABELS: Record<ReviewState | "none", string> = {
  none: "Not reviewed",
  queued: "Queued",
  running: "Reviewing",
  current: "Current",
  outdated: "Outdated",
  failed: "Failed",
};

/** The review state of a pull request in the list, or "Not reviewed". */
export function reviewStateLabel(review: PullRequestReview | null) {
  return REVIEW_STATE_LABELS[review?.state ?? "none"];
}

/** A short label for a review's execution status, or its outcome once it succeeded. */
export function reviewStatusLabel(status: ReviewStatus, quality: ReviewQualityState): string {
  switch (status) {
    case "queued":
      return "Queued";
    case "running":
      return "Reviewing";
    case "retry_wait":
      return "Retrying";
    case "succeeded":
      return quality === "nothing_to_review" ? "Nothing to review" : "Reviewed";
    case "failed":
      return "Failed";
    case "canceled":
      return "Canceled";
  }
}

const CHANGE_LABELS: Record<FileChange, string> = {
  added: "Added",
  modified: "Modified",
  renamed: "Renamed",
  removed: "Removed",
};

/** How a pull request changed a file, in words. */
export function changeLabel(change: FileChange) {
  return CHANGE_LABELS[change];
}

/**
 * `url` when it is an `https` link, otherwise null. Links from GitHub (pull requests, cited lines,
 * installation settings) are only followed when they cannot run script.
 */
export function externalUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    return new URL(url).protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}
