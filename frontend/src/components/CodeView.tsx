"use client";

import Link from "next/link";
import { useEffect, useRef } from "react";

import {
  browseUrl,
  type LineRange,
  MAX_LINES_PER_REQUEST,
  useFileLines,
} from "@/lib/api/browse";
import { ApiError, errorMessage } from "@/lib/api/client";
import { shortSha } from "@/lib/api/repositories";

// Lines shown above a requested range when it lies past the first window of a long file.
const CONTEXT_LINES = 100;

function rangeText(range: LineRange) {
  return range.start === range.end
    ? `line ${range.start.toLocaleString()}`
    : `lines ${range.start.toLocaleString()}–${range.end.toLocaleString()}`;
}

/**
 * Numbered lines of code, starting at `firstLine`. With `anchors`, each row has the id `L<n>`
 * and its number links to `#L<n>`; lines inside `highlight` are marked.
 */
export function CodeLines({
  lines,
  firstLine,
  highlight = null,
  anchors = false,
}: {
  lines: string[];
  firstLine: number;
  highlight?: LineRange | null;
  anchors?: boolean;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse font-mono text-xs leading-5">
        <tbody>
          {lines.map((text, index) => {
            const number = firstLine + index;
            const marked =
              highlight !== null && number >= highlight.start && number <= highlight.end;
            return (
              <tr
                key={number}
                id={anchors ? `L${number}` : undefined}
                className={`scroll-mt-24 ${marked ? "bg-yellow-100 dark:bg-yellow-900/40" : ""}`}
              >
                <td className="w-px px-3 text-right align-top text-zinc-400 tabular-nums select-none">
                  {anchors ? (
                    <a
                      href={`#L${number}`}
                      className="hover:text-zinc-700 hover:underline dark:hover:text-zinc-200"
                    >
                      {number}
                    </a>
                  ) : (
                    number
                  )}
                </td>
                <td className="pr-4 align-top whitespace-pre [tab-size:4]">
                  <code>{text}</code>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const MORE_BUTTON =
  "w-full border-zinc-200 bg-zinc-50 px-3 py-1.5 text-left text-xs font-medium text-zinc-700 hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-300 dark:hover:bg-zinc-800";

/**
 * A file of a snapshot with line numbers and line anchors. `range` is highlighted and scrolled
 * into view. Files load at most 1,000 lines at a time; a range past the first 1,000 lines loads
 * the window around it, and buttons load the lines before and after.
 */
export function CodeView({
  snapshotId,
  path,
  range,
}: {
  snapshotId: string;
  path: string;
  range: LineRange | null;
}) {
  const firstLine =
    range && range.end > MAX_LINES_PER_REQUEST ? Math.max(1, range.start - CONTEXT_LINES) : 1;
  const file = useFileLines(snapshotId, path, firstLine);
  const pages = file.data?.pages ?? [];
  const first = pages[0];
  const last = pages[pages.length - 1];

  // Scroll to the highlighted range once, when its lines first appear.
  const highlightStart = range?.start ?? null;
  const loaded = first !== undefined;
  const scrolledTo = useRef<string | null>(null);
  useEffect(() => {
    if (!loaded || highlightStart === null) return;
    const target = `${path}#${highlightStart}`;
    if (scrolledTo.current === target) return;
    scrolledTo.current = target;
    document.getElementById(`L${highlightStart}`)?.scrollIntoView({ block: "center" });
  }, [loaded, path, highlightStart]);

  if (!first || !last) {
    if (file.error) {
      const missing = file.error instanceof ApiError && file.error.status === 404;
      return (
        <div role="alert" className="flex flex-col gap-2 text-sm text-red-700 dark:text-red-400">
          <p>
            {missing
              ? `${path} does not exist in this version.`
              : `Could not open ${path}: ${errorMessage(file.error)}`}
          </p>
          {range && !missing && (
            <Link href={browseUrl(snapshotId, path)} className="self-start font-medium underline">
              Open the file from the first line
            </Link>
          )}
        </div>
      );
    }
    return <p className="text-sm text-zinc-500">Loading {path}…</p>;
  }

  const lines = pages.flatMap((page) => page.lines);
  const previous: LineRange = {
    start: Math.max(1, first.start_line - MAX_LINES_PER_REQUEST),
    end: first.start_line - 1,
  };
  const next: LineRange = {
    start: last.end_line + 1,
    end: Math.min(last.line_count, last.end_line + MAX_LINES_PER_REQUEST),
  };

  return (
    <section aria-label={`File ${path}`} className="flex flex-col gap-2">
      <div className="flex flex-col gap-1">
        <h2 className="font-mono text-sm font-semibold break-all">{path}</h2>
        <p className="text-xs text-zinc-500">
          {first.line_count.toLocaleString()} {first.line_count === 1 ? "line" : "lines"}
          {first.language ? ` · ${first.language}` : ""} · commit{" "}
          <code title={first.commit_sha}>{shortSha(first.commit_sha)}</code>
          {range && <> · {rangeText(range)} highlighted</>}
        </p>
      </div>
      {first.line_count === 0 ? (
        <p className="text-sm text-zinc-600 dark:text-zinc-400">This file is empty.</p>
      ) : (
        <div className="overflow-hidden rounded-lg border border-zinc-200 dark:border-zinc-800">
          {previous.end >= 1 && (
            <button
              type="button"
              onClick={() => void file.fetchPreviousPage()}
              disabled={file.isFetchingPreviousPage}
              className={`border-b ${MORE_BUTTON}`}
            >
              {file.isFetchingPreviousPage
                ? "Loading…"
                : `Show ${rangeText(previous)}`}
            </button>
          )}
          <CodeLines lines={lines} firstLine={first.start_line} highlight={range} anchors />
          {next.start <= last.line_count && (
            <button
              type="button"
              onClick={() => void file.fetchNextPage()}
              disabled={file.isFetchingNextPage}
              className={`border-t ${MORE_BUTTON}`}
            >
              {file.isFetchingNextPage
                ? "Loading…"
                : `Show ${rangeText(next)}`}
            </button>
          )}
        </div>
      )}
    </section>
  );
}
