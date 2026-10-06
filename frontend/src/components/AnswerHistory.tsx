"use client";

import Link from "next/link";

import { RunStatusBadge } from "@/components/RunStatusBadge";
import { errorMessage } from "@/lib/api/client";
import { useRunHistory } from "@/lib/api/questions";
import { shortSha } from "@/lib/api/repositories";

/** The questions asked about a repository, newest first, each linking to its answer. */
export function AnswerHistory({ repositoryId }: { repositoryId: string }) {
  const history = useRunHistory(repositoryId);
  const runs = history.data?.pages.flatMap((page) => page.items) ?? [];

  if (!history.data) {
    if (history.error) {
      return (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          Could not load earlier questions: {errorMessage(history.error)}
        </p>
      );
    }
    return <p className="text-sm text-zinc-500">Loading earlier questions…</p>;
  }

  if (runs.length === 0) {
    return (
      <p className="text-sm text-zinc-600 dark:text-zinc-400">No questions have been asked yet.</p>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <ul className="divide-y divide-zinc-200 rounded-lg border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
        {runs.map((run) => (
          <li key={run.id}>
            <Link
              href={`/answers/${run.id}`}
              className="flex flex-col gap-1 px-4 py-3 hover:bg-zinc-50 dark:hover:bg-zinc-900"
            >
              <span className="flex items-start justify-between gap-3">
                <span className="line-clamp-2 font-medium break-words">{run.question}</span>
                <RunStatusBadge status={run.status} quality={run.quality_state} />
              </span>
              <span className="text-xs text-zinc-500">
                Commit <code title={run.commit_sha}>{shortSha(run.commit_sha)}</code> ·{" "}
                <time dateTime={run.created_at}>{new Date(run.created_at).toLocaleString()}</time>
              </span>
            </Link>
          </li>
        ))}
      </ul>
      {history.hasNextPage && (
        <button
          type="button"
          onClick={() => void history.fetchNextPage()}
          disabled={history.isFetchingNextPage}
          className="self-center rounded-md border border-zinc-300 px-4 py-1.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
        >
          {history.isFetchingNextPage ? "Loading…" : "Load more"}
        </button>
      )}
    </div>
  );
}
