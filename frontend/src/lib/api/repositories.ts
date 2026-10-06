import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema";

export type Repository = components["schemas"]["RepositoryOut"];
export type RepositoryState = Repository["state"];
export type ActiveSnapshot = NonNullable<Repository["active_snapshot"]>;
export type JobError = components["schemas"]["JobError"];
export type GitHubRepository = components["schemas"]["GitHubRepositoryOut"];
export type CoverageEntry = components["schemas"]["CoverageEntryOut"];

const ACTIVE_JOB_STATUSES: readonly string[] = ["queued", "running", "retry_wait"];

/** Whether a job status means the job has not finished yet. */
export function isActiveJob(status: string) {
  return ACTIVE_JOB_STATUSES.includes(status);
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

export function useRepository(repositoryId: string) {
  return useQuery({
    queryKey: ["repositories", repositoryId],
    queryFn: () =>
      unwrap(
        api.GET("/v1/repositories/{repository_id}", {
          params: { path: { repository_id: repositoryId } },
        }),
      ),
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
