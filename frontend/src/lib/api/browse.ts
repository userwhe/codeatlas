import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema";

export type TreeEntry = components["schemas"]["TreeEntryOut"];
export type FileLines = components["schemas"]["FileLinesOut"];
export type SearchMode = components["schemas"]["SearchRequest"]["mode"];
export type SearchResult = components["schemas"]["SearchResultOut"];

/** The API returns at most this many lines of a file per request. */
export const MAX_LINES_PER_REQUEST = 1000;

export const SEARCH_MODES: readonly SearchMode[] = ["text", "path", "symbol", "docs"];
export const MAX_QUERY_CHARS = 200;
const SEARCH_LIMIT = 50;

/** An inclusive range of 1-based line numbers. */
export interface LineRange {
  start: number;
  end: number;
}

/** Reads `lines=<start>-<end>` (or `lines=<n>`) from a URL; anything else is no range. */
export function parseLineRange(value: string | null): LineRange | null {
  const match = value ? /^(\d+)(?:-(\d+))?$/.exec(value) : null;
  if (!match) return null;
  const start = Number(match[1]);
  const end = match[2] ? Number(match[2]) : start;
  if (start < 1 || end < start) return null;
  return { start, end };
}

/** The file browser URL for a path in a snapshot, optionally at a line range. */
export function browseUrl(snapshotId: string, path?: string, range?: LineRange | null) {
  const query = new URLSearchParams();
  if (path) query.set("path", path);
  if (path && range) query.set("lines", `${range.start}-${range.end}`);
  const search = query.toString();
  return `/snapshots/${snapshotId}/browse${search ? `?${search}` : ""}`;
}

/** The immediate children of a directory (`""` is the root). Fetched only while `enabled`. */
export function useTree(snapshotId: string, path: string, enabled = true) {
  return useQuery({
    queryKey: ["snapshots", snapshotId, "tree", path],
    queryFn: () =>
      unwrap(
        api.GET("/v1/snapshots/{snapshot_id}/tree", {
          params: { path: { snapshot_id: snapshotId }, query: { path } },
        }),
      ),
    enabled,
    // A snapshot never changes once it is ready.
    staleTime: Infinity,
  });
}

interface LinesParam {
  start: number;
  end?: number;
}

/**
 * Lines of a file, one window of at most 1,000 lines per page, starting at `firstLine`.
 * `fetchPreviousPage` loads the window before the first page and `fetchNextPage` the one after
 * the last, so long files load around the requested lines.
 */
export function useFileLines(snapshotId: string, path: string, firstLine: number) {
  return useInfiniteQuery({
    queryKey: ["snapshots", snapshotId, "file", path, firstLine],
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/v1/snapshots/{snapshot_id}/file", {
          params: {
            path: { snapshot_id: snapshotId },
            query: { path, start_line: pageParam.start, end_line: pageParam.end },
          },
        }),
      ),
    initialPageParam: { start: firstLine } as LinesParam,
    getNextPageParam: (last): LinesParam | undefined =>
      last.end_line < last.line_count ? { start: last.end_line + 1 } : undefined,
    getPreviousPageParam: (first): LinesParam | undefined =>
      first.start_line > 1
        ? {
            start: Math.max(1, first.start_line - MAX_LINES_PER_REQUEST),
            end: first.start_line - 1,
          }
        : undefined,
    staleTime: Infinity,
  });
}

/** Search results for a query in one snapshot. Runs only when `query` is valid. */
export function useSearch(snapshotId: string, mode: SearchMode, query: string) {
  return useQuery({
    queryKey: ["snapshots", snapshotId, "search", mode, query],
    queryFn: () =>
      unwrap(
        api.POST("/v1/search", {
          body: { snapshot_id: snapshotId, query, mode, limit: SEARCH_LIMIT },
        }),
      ),
    enabled: query.length > 0 && query.length <= MAX_QUERY_CHARS,
    staleTime: Infinity,
  });
}
