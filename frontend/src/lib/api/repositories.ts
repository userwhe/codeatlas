import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema";

export type Repository = components["schemas"]["RepositoryOut"];
export type RepositoryState = Repository["state"];
export type ActiveSnapshot = NonNullable<Repository["active_snapshot"]>;
export type JobError = components["schemas"]["JobError"];
export type LatestJob = components["schemas"]["LatestJobOut"];
export type LatestPush = components["schemas"]["LatestPushOut"];
export type JobTrigger = LatestJob["trigger"];
export type RepositoryAccess = components["schemas"]["AccessOut"];
export type AutomaticUpdates = components["schemas"]["AutomaticUpdatesOut"];
export type GitHubRepository = components["schemas"]["GitHubRepositoryOut"];
export type CoverageEntry = components["schemas"]["CoverageEntryOut"];

const ACTIVE_JOB_STATUSES: readonly string[] = ["queued", "running", "retry_wait"];

/** Whether a job status means the job has not finished yet. */
export function isActiveJob(status: string) {
  return ACTIVE_JOB_STATUSES.includes(status);
}

const TRIGGER_LABELS: Record<JobTrigger, string> = {
  user: "Indexed by you",
  push: "After a push",
  check: "Daily check",
};

/** What started an indexing run, or the run that built a version (FR-009). */
export function triggerLabel(trigger: JobTrigger) {
  return TRIGGER_LABELS[trigger];
}

const INDEXING_STATUS_LABELS: Record<string, string> = {
  queued: "Waiting",
  retry_wait: "Waiting to retry",
  running: "Indexing",
  succeeded: "Indexed",
  failed: "Failed",
  canceled: "Canceled",
};

/** The state of an indexing run in words: waiting, indexing, indexed, or failed (FR-009). */
export function indexingStatusLabel(status: string) {
  return INDEXING_STATUS_LABELS[status] ?? status;
}

const ACCESS_LOSS_REASONS: Record<string, string> = {
  repository_not_visible:
    "Your GitHub account can no longer see this repository. It may have been deleted, or your access to it may have been removed.",
  app_not_installed:
    "The CodeAtlas GitHub App is no longer installed with access to this repository.",
  installation_not_accessible:
    "Your GitHub account can no longer use the CodeAtlas GitHub App installation that covers this repository. For example, you may have been removed from the organization, or the installation may be suspended.",
  installation_cannot_read:
    "The CodeAtlas GitHub App installation can no longer read this repository's contents.",
  app_uninstalled:
    "The CodeAtlas GitHub App was uninstalled from the account that owns this repository.",
  app_suspended:
    "The CodeAtlas GitHub App was suspended on the account that owns this repository.",
  repository_removed_from_installation:
    "This repository was removed from the repositories the CodeAtlas GitHub App can access.",
};

/** Why access to a repository was lost, in plain English (FR-014). */
export function accessLossReasonText(reason: string | null | undefined) {
  return (
    (reason ? ACCESS_LOSS_REASONS[reason] : undefined) ??
    "GitHub no longer allows CodeAtlas to read this repository."
  );
}

/**
 * Whether automatic updates are paused until the owner accepts the external processing
 * disclosure, because the repository became private on GitHub (FR-017). A re-index request must
 * then accept it.
 */
export function isPausedForDisclosure(automaticUpdates: AutomaticUpdates) {
  return (
    automaticUpdates.state === "paused" &&
    automaticUpdates.reason === "external_processing_not_accepted"
  );
}

/** The first 7 characters of a commit SHA, as GitHub shows it. */
export function shortSha(sha: string) {
  return sha.slice(0, 7);
}

const INDEXING_POLL_MS = 5000;

/** The workspace's connected repositories, one page per "Load more". */
export function useRepositories() {
  return useInfiniteQuery({
    queryKey: ["repositories", "list"],
    queryFn: ({ pageParam }) =>
      unwrap(api.GET("/v1/repositories", { params: { query: { cursor: pageParam } } })),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor,
    // Keep state badges current while any listed repository is indexing.
    refetchInterval: (query) =>
      query.state.data?.pages.some((page) => page.items.some((item) => item.state === "indexing"))
        ? INDEXING_POLL_MS
        : false,
  });
}

const ACCESS_CHECK_POLL_MS = 1500;

/**
 * One repository. With `followAccessChecks`, it is polled while its access is lost and an
 * indexing run is active: that run checks access before anything else, but its progress cannot
 * be read while access is lost (FR-014), so the repository shows when the run restores access
 * or ends.
 */
export function useRepository(
  repositoryId: string,
  { followAccessChecks = false }: { followAccessChecks?: boolean } = {},
) {
  return useQuery({
    queryKey: ["repositories", repositoryId],
    queryFn: () =>
      unwrap(
        api.GET("/v1/repositories/{repository_id}", {
          params: { path: { repository_id: repositoryId } },
        }),
      ),
    refetchInterval: (query) => {
      const repository = query.state.data;
      const job = repository?.latest_indexing_job;
      const checking = repository?.state === "access_lost" && job != null && isActiveJob(job.status);
      return followAccessChecks && checking ? ACCESS_CHECK_POLL_MS : false;
    },
  });
}

/** Repositories the user can connect through installations of the GitHub App. */
export function useGitHubRepositories() {
  return useInfiniteQuery({
    queryKey: ["github-repositories"],
    queryFn: ({ pageParam }) =>
      unwrap(api.GET("/v1/github/repositories", { params: { query: { cursor: pageParam } } })),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor,
  });
}

export function useSnapshot(snapshotId: string) {
  return useQuery({
    queryKey: ["snapshots", snapshotId],
    queryFn: () =>
      unwrap(
        api.GET("/v1/snapshots/{snapshot_id}", { params: { path: { snapshot_id: snapshotId } } }),
      ),
  });
}

/** The skipped entries of a ready snapshot, one page per "Load more". */
export function useCoverage(snapshotId: string) {
  return useInfiniteQuery({
    queryKey: ["snapshots", snapshotId, "coverage"],
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/v1/snapshots/{snapshot_id}/coverage", {
          params: { path: { snapshot_id: snapshotId }, query: { cursor: pageParam } },
        }),
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor,
  });
}

export type SnapshotListItem = components["schemas"]["SnapshotOut"];

/** The ready indexed versions of a repository, newest first, one page per "Load more". */
export function useSnapshots(repositoryId: string) {
  return useInfiniteQuery({
    queryKey: ["repositories", repositoryId, "snapshots"],
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/v1/repositories/{repository_id}/snapshots", {
          params: {
            path: { repository_id: repositoryId },
            query: { cursor: pageParam, limit: 100 },
          },
        }),
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor,
  });
}
