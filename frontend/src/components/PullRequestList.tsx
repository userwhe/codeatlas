"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type ReactNode, useState } from "react";

import { RelativeTime } from "@/components/RelativeTime";
import { ApiError } from "@/lib/api/client";
import { useUsage } from "@/lib/api/questions";
import { shortSha } from "@/lib/api/repositories";
import {
  externalUrl,
  type PullRequest,
  type ReviewState,
  reviewErrorMessage,
  reviewStateLabel,
  usePullRequests,
  useRequestReview,
} from "@/lib/api/reviews";

const STATE_STYLES: Record<ReviewState | "none", string> = {
  none: "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  queued: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-200",
  running: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-200",
  current: "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-200",
  outdated: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
  failed: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200",
};

const PRIMARY_BUTTON =
  "rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300";
const SECONDARY_BUTTON =
  "rounded-md border border-zinc-300 px-3 py-1.5 text-sm font-medium hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800";

/**
 * The repository's open pull requests, read live from GitHub, each with its review state and an
 * action: review it, view its review, or review it again.
 */
export function PullRequestList({ repositoryId }: { repositoryId: string }) {
  const router = useRouter();
  const pullRequests = usePullRequests(repositoryId);
  const request = useRequestReview();
  // Pages are loaded once and kept; this is the one on screen.
  const [pageIndex, setPageIndex] = useState(0);

  if (!pullRequests.data) {
    if (pullRequests.error) {
      return (
        <ListProblem error={pullRequests.error} onRetry={() => void pullRequests.refetch()} />
      );
    }
    return <p className="text-sm text-zinc-500">Loading pull requests…</p>;
  }

  const pages = pullRequests.data.pages;
  const index = Math.min(pageIndex, pages.length - 1);
  const items = pages[index]?.items ?? [];
  const hasNext = index < pages.length - 1 || pullRequests.hasNextPage;
  // A request is on its way, or its review page is opening.
  const busy = request.isPending || request.isSuccess;
  const requestedNumber = request.variables?.pullRequestNumber ?? null;

  function review(number: number) {
    if (busy) return;
    request.mutate(
      {
        repositoryId,
        pullRequestNumber: number,
        // A new key for each submission attempt; a replay of the same request queues it once.
        idempotencyKey: crypto.randomUUID(),
      },
      { onSuccess: ({ run_id }) => router.push(`/reviews/${run_id}`) },
    );
  }

  async function nextPage() {
    if (index >= pages.length - 1) {
      const result = await pullRequests.fetchNextPage();
      if ((result.data?.pages.length ?? 0) <= index + 1) return;
    }
    setPageIndex(index + 1);
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        Open pull requests, most recently updated first. A review reads the pull request&apos;s
        changes at its current head and cites the lines it relies on.{" "}
        <ReviewAllowance />
      </p>

      {items.length === 0 ? (
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          {index === 0 ? "There are no open pull requests." : "No more open pull requests."}
        </p>
      ) : (
        <ul className="divide-y divide-zinc-200 rounded-lg border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
          {items.map((pullRequest) => {
            const requested = requestedNumber === pullRequest.number;
            return (
              <PullRequestRow
                key={pullRequest.number}
                pullRequest={pullRequest}
                busy={busy}
                requesting={requested && busy}
                error={requested && request.error ? reviewErrorMessage(request.error) : null}
                onReview={() => review(pullRequest.number)}
              />
            );
          })}
        </ul>
      )}

      {pullRequests.error && (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          Could not load the pull requests: {reviewErrorMessage(pullRequests.error)}
        </p>
      )}

      {(index > 0 || hasNext) && (
        <nav aria-label="Pull request pages" className="flex items-center justify-center gap-3">
          <button
            type="button"
            onClick={() => setPageIndex(index - 1)}
            disabled={index === 0}
            className={SECONDARY_BUTTON}
          >
            Previous page
          </button>
          <span className="text-xs text-zinc-500 tabular-nums">Page {index + 1}</span>
          <button
            type="button"
            onClick={() => void nextPage()}
            disabled={!hasNext || pullRequests.isFetchingNextPage}
            className={SECONDARY_BUTTON}
          >
            {pullRequests.isFetchingNextPage ? "Loading…" : "Next page"}
          </button>
        </nav>
      )}
    </div>
  );
}

/** Today's remaining reviews of the workspace. */
function ReviewAllowance() {
  const { data: usage } = useUsage();
  if (!usage) return null;
  const left = Math.max(0, usage.reviews_limit - usage.reviews_used);
  return (
    <span className={left === 0 ? "font-medium text-amber-700 dark:text-amber-400" : undefined}>
      <span className="tabular-nums">
        {left} of {usage.reviews_limit}
      </span>{" "}
      reviews left today.
    </span>
  );
}

function Label({ children }: { children: ReactNode }) {
  return (
    <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-xs font-medium text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">
      {children}
    </span>
  );
}

function PullRequestRow({
  pullRequest,
  busy,
  requesting,
  error,
  onReview,
}: {
  pullRequest: PullRequest;
  busy: boolean;
  requesting: boolean;
  error: string | null;
  onReview: () => void;
}) {
  const review = pullRequest.review;
  const url = externalUrl(pullRequest.html_url);
  const number = `#${pullRequest.number}`;

  let action: ReactNode;
  if (review === null) {
    action = (
      <button type="button" onClick={onReview} disabled={busy} className={PRIMARY_BUTTON}>
        {requesting ? "Requesting…" : "Review"}
      </button>
    );
  } else if (review.state === "outdated" || review.state === "failed") {
    action = (
      <>
        <Link
          href={`/reviews/${review.run_id}`}
          className="text-sm text-zinc-600 underline dark:text-zinc-400"
        >
          Last review
        </Link>
        <button type="button" onClick={onReview} disabled={busy} className={SECONDARY_BUTTON}>
          {requesting ? "Requesting…" : "Review again"}
        </button>
      </>
    );
  } else {
    action = (
      <Link href={`/reviews/${review.run_id}`} className={SECONDARY_BUTTON}>
        View review
      </Link>
    );
  }

  return (
    <li className="flex flex-col gap-2 px-4 py-3">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 flex-col gap-1">
          <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            {url ? (
              <a
                href={url}
                target="_blank"
                rel="noreferrer"
                title="Open on GitHub"
                className="text-zinc-500 tabular-nums hover:underline"
              >
                {number}
              </a>
            ) : (
              <span className="text-zinc-500 tabular-nums">{number}</span>
            )}
            <span className="min-w-0 font-medium break-words">{pullRequest.title}</span>
            {pullRequest.draft && <Label>Draft</Label>}
            {pullRequest.is_fork && <Label>Fork</Label>}
          </p>
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-zinc-500">
            <span>By {pullRequest.author}</span>
            <span aria-hidden="true">·</span>
            <span className="break-all">
              <span className="sr-only">
                Merging {pullRequest.head_ref} into {pullRequest.base_ref}
              </span>
              <span aria-hidden="true">
                <code>{pullRequest.base_ref}</code> ← <code>{pullRequest.head_ref}</code>
              </span>
            </span>
            <span aria-hidden="true">·</span>
            <span>
              Head <code title={pullRequest.head_sha}>{shortSha(pullRequest.head_sha)}</code>
            </span>
            <span aria-hidden="true">·</span>
            <span>
              Updated <RelativeTime value={pullRequest.updated_at} />
            </span>
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-3">
          <span
            className={`rounded-full px-2 py-0.5 text-xs font-medium ${STATE_STYLES[review?.state ?? "none"]}`}
          >
            {reviewStateLabel(review)}
          </span>
          {action}
        </div>
      </div>
      {error && (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          {error}
        </p>
      )}
    </li>
  );
}

/**
 * Why the list could not be loaded. A missing Pull requests permission links to the GitHub App
 * installation's settings, where the account owner approves it.
 */
function ListProblem({ error, onRetry }: { error: Error; onRetry: () => void }) {
  const apiError = error instanceof ApiError ? error : null;
  const permissionMissing = apiError?.code === "pull_requests_permission_missing";
  const settingsUrl =
    permissionMissing && typeof apiError.details.settings_url === "string"
      ? externalUrl(apiError.details.settings_url)
      : null;
  const retryable = apiError === null || apiError.retryable;

  if (permissionMissing) {
    return (
      <div
        role="alert"
        className="flex flex-col gap-2 rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100"
      >
        <p className="font-medium">Pull requests cannot be listed yet.</p>
        <p>{reviewErrorMessage(error)}</p>
        <div className="flex flex-wrap items-center gap-4">
          {settingsUrl && (
            <a href={settingsUrl} target="_blank" rel="noreferrer" className="font-medium underline">
              Open the installation settings on GitHub
            </a>
          )}
          <button type="button" onClick={onRetry} className="font-medium underline">
            Check again
          </button>
        </div>
      </div>
    );
  }

  return (
    <div role="alert" className="flex flex-col gap-2 text-sm text-red-700 dark:text-red-400">
      <p>Could not load the pull requests: {reviewErrorMessage(error)}</p>
      {retryable && (
        <button type="button" onClick={onRetry} className="self-start font-medium underline">
          Try again
        </button>
      )}
    </div>
  );
}
