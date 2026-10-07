"use client";

import Link from "next/link";

import { RiskLevelBadge } from "@/components/RiskLevelBadge";
import { errorMessage } from "@/lib/api/client";
import { shortSha } from "@/lib/api/repositories";
import { type ReviewQualityState, reviewStatusLabel, useReviewHistory } from "@/lib/api/reviews";

/** The history holds only reviews, so a question's quality state never appears in it. */
function reviewQuality(quality: string | null): ReviewQualityState {
  return quality === "reviewed" || quality === "nothing_to_review" ? quality : null;
}

/**
 * The repository's reviews, newest first, each linking to its review page. Reviews of closed and
 * merged pull requests are listed too, after the pull requests leave the open list (FR-024).
 */
export function ReviewHistory({ repositoryId }: { repositoryId: string }) {
  const history = useReviewHistory(repositoryId);
  const runs = history.data?.pages.flatMap((page) => page.items) ?? [];

  if (!history.data) {
    if (history.error) {
      return (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          Could not load earlier reviews: {errorMessage(history.error)}
        </p>
      );
    }
    return <p className="text-sm text-zinc-500">Loading earlier reviews…</p>;
  }

  if (runs.length === 0) {
    return <p className="text-sm text-zinc-600 dark:text-zinc-400">No reviews yet.</p>;
  }

  return (
    <div className="flex flex-col gap-3">
      <ul className="divide-y divide-zinc-200 rounded-lg border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
        {runs.map((run) => (
          <li key={run.id}>
            <Link
              href={`/reviews/${run.id}`}
              className="flex flex-col gap-1 px-4 py-3 hover:bg-zinc-50 dark:hover:bg-zinc-900"
            >
              <span className="flex items-start justify-between gap-3">
                <span className="line-clamp-2 font-medium break-words">
                  #{run.pull_request_number} {run.pull_request_title}
                </span>
                {run.overall_risk_level ? (
                  <RiskLevelBadge level={run.overall_risk_level} />
                ) : (
                  <span className="shrink-0 text-xs text-zinc-500">
                    {reviewStatusLabel(run.status, reviewQuality(run.quality_state))}
                  </span>
                )}
              </span>
              <span className="text-xs text-zinc-500">
                Head <code title={run.commit_sha}>{shortSha(run.commit_sha)}</code> ·{" "}
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
