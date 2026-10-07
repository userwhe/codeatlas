"use client";

import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Fragment, type ReactNode, useCallback, useState } from "react";

import { AccessLostNotice } from "@/components/AccessLostNotice";
import { reasonLabel } from "@/components/CoverageTable";
import { JobProgress } from "@/components/JobProgress";
import { citationLines, ReviewCitation } from "@/components/ReviewCitation";
import { RiskLevelBadge } from "@/components/RiskLevelBadge";
import { ApiError, errorMessage, isAccessLost } from "@/lib/api/client";
import { isActiveJob, shortSha } from "@/lib/api/repositories";
import {
  type CandidateTest,
  changeLabel,
  type ChecklistItem,
  type CoverageFile,
  externalUrl,
  type FileChange,
  type Review,
  type ReviewCitation as Citation,
  type ReviewCoverage,
  reviewErrorMessage,
  type ReviewQualityState,
  type ReviewRun,
  type ReviewStatus,
  reviewStatusLabel,
  type ReviewTests,
  type Risk,
  useFreshness,
  useRequestReview,
  useReview,
  useReviewMarkdown,
} from "@/lib/api/reviews";

export function ReviewDetail({ runId }: { runId: string }) {
  const queryClient = useQueryClient();
  const { data: run, error } = useReview(runId);

  // When the review job finishes, load the review (or error) and refresh the pull request list
  // and usage.
  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["analysis-runs"] });
    void queryClient.invalidateQueries({ queryKey: ["pull-requests"] });
    void queryClient.invalidateQueries({ queryKey: ["usage"] });
  }, [queryClient]);

  // Checked before the cached review, which stays in the cache after access is lost.
  if (isAccessLost(error)) return <AccessLostNotice error={error} subject="This review" />;

  if (!run) {
    if (error) {
      const missing = error instanceof ApiError && (error.status === 404 || error.status === 422);
      return (
        <Problem>
          {missing
            ? "This review does not exist or is no longer available."
            : `Could not load the review: ${errorMessage(error)}`}
        </Problem>
      );
    }
    return <p className="text-sm text-zinc-500">Loading review…</p>;
  }

  // The same address space holds answers to questions, which have their own page.
  if (run.kind !== "pull_request_review") {
    return (
      <div className="flex flex-col gap-4">
        <RepositoryLink repositoryId={run.repository_id}>Repository</RepositoryLink>
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          This is an answer to a question, not a pull request review.{" "}
          <Link href={`/answers/${run.id}`} className="font-medium underline">
            Open the answer
          </Link>
        </p>
      </div>
    );
  }

  const active = isActiveJob(run.status);

  return (
    <div className="flex flex-col gap-10">
      <ReviewHeader run={run} />

      <FreshnessBanner run={run} finished={!active} />

      {active &&
        (run.job_id ? (
          <JobProgress key={run.job_id} jobId={run.job_id} onFinished={refresh} />
        ) : (
          <p className="text-sm text-zinc-500">Waiting for the review to start…</p>
        ))}

      {run.status === "succeeded" &&
        (run.review && run.coverage ? (
          <ReviewBody run={run} review={run.review} coverage={run.coverage} />
        ) : (
          <p className="text-sm text-zinc-600 dark:text-zinc-400">The review is not available.</p>
        ))}

      {(run.status === "failed" || run.status === "canceled") && <RunProblem run={run} />}
    </div>
  );
}

function Problem({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-col gap-4">
      <Link href="/repositories" className="text-sm text-zinc-600 hover:underline dark:text-zinc-400">
        ← All repositories
      </Link>
      <p role="alert" className="text-sm text-red-700 dark:text-red-400">
        {children}
      </p>
    </div>
  );
}

function RepositoryLink({ repositoryId, children }: { repositoryId: string; children: ReactNode }) {
  return (
    <Link
      href={`/repositories/${repositoryId}`}
      className="self-start text-sm text-zinc-600 hover:underline dark:text-zinc-400"
    >
      ← {children}
    </Link>
  );
}

const ACTIVE = "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-200";
const NEUTRAL = "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300";

function statusStyle(status: ReviewStatus, quality: ReviewQualityState) {
  switch (status) {
    case "succeeded":
      return quality === "nothing_to_review"
        ? NEUTRAL
        : "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-200";
    case "failed":
      return "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200";
    case "canceled":
      return NEUTRAL;
    default:
      return ACTIVE;
  }
}

function Label({ children }: { children: ReactNode }) {
  return (
    <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-xs font-medium text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">
      {children}
    </span>
  );
}

function Sha({ sha }: { sha: string }) {
  return (
    <code title={sha} className="font-medium text-zinc-900 dark:text-zinc-100">
      {shortSha(sha)}
    </code>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-zinc-500">{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/** The pull request as it was when the review was requested, and the three pinned commits. */
function ReviewHeader({ run }: { run: ReviewRun }) {
  const pullRequest = run.pull_request;
  // The pull request's state on GitHub, from the same query as the freshness banner.
  const freshness = useFreshness(run.id, !isActiveJob(run.status));
  const state = freshness.data?.pull_request_state;
  const pullRequestUrl = externalUrl(pullRequest.html_url);
  // A fork's branch is named with its repository, as GitHub shows it.
  const head =
    pullRequest.is_fork && pullRequest.head_repository
      ? `${pullRequest.head_repository}:${pullRequest.head_ref}`
      : pullRequest.head_ref;
  const title = (
    <>
      <span className="text-zinc-500">#{pullRequest.number}</span> {pullRequest.title}
    </>
  );
  const mergeBase = run.commits.merge_base_sha;

  return (
    <header className="flex flex-col gap-3">
      <RepositoryLink repositoryId={run.repository_id}>{run.repository_full_name}</RepositoryLink>
      <div className="flex flex-col gap-1">
        <p className="text-xs font-medium tracking-wide text-zinc-500 uppercase">
          Pull request review
        </p>
        <h1 className="text-2xl font-semibold tracking-tight break-words">
          {pullRequestUrl ? (
            <a href={pullRequestUrl} target="_blank" rel="noreferrer" className="hover:underline">
              {title}
            </a>
          ) : (
            title
          )}
        </h1>
      </div>
      <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-zinc-600 dark:text-zinc-400">
        <span
          className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${statusStyle(run.status, run.quality_state)}`}
        >
          {reviewStatusLabel(run.status, run.quality_state)}
        </span>
        {(state === "merged" || state === "closed") && (
          <span
            className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${STATE_STYLES[state]}`}
          >
            {state === "merged" ? "Merged" : "Closed"}
          </span>
        )}
        <span>
          By <span className="font-medium text-zinc-900 dark:text-zinc-100">{pullRequest.author}</span>
        </span>
        <span aria-hidden="true">·</span>
        <span className="break-all">
          <span className="sr-only">
            Merging {head} into {pullRequest.base_ref}
          </span>
          <span aria-hidden="true">
            <code>{pullRequest.base_ref}</code> ← <code>{head}</code>
          </span>
        </span>
        {pullRequest.is_fork && <Label>Fork</Label>}
        {pullRequest.draft && <Label>Draft</Label>}
        <span aria-hidden="true">·</span>
        <span>
          Requested{" "}
          <time dateTime={run.created_at}>{new Date(run.created_at).toLocaleString()}</time>
        </span>
      </p>
      <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-3">
        <Field label="Base">
          <Sha sha={run.commits.base_sha} />
        </Field>
        <Field label="Head">
          <Sha sha={run.commits.head_sha} />
        </Field>
        <Field label="Merge base">
          {mergeBase ? (
            <Sha sha={mergeBase} />
          ) : (
            <span className="text-zinc-500">
              {isActiveJob(run.status) ? "Not resolved yet" : "Not resolved"}
            </span>
          )}
        </Field>
      </dl>
    </header>
  );
}

const BANNER = "flex flex-col gap-2 rounded-md border px-4 py-3 text-sm";
const NOTICE_BANNER = `${BANNER} border-amber-300 bg-amber-50 text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100`;
const STATE_STYLES = {
  merged: "bg-purple-100 text-purple-800 dark:bg-purple-950 dark:text-purple-200",
  closed: "bg-zinc-200 text-zinc-800 dark:bg-zinc-800 dark:text-zinc-200",
} as const;
const MERGED_BANNER = `${BANNER} border-purple-300 bg-purple-50 text-purple-950 dark:border-purple-800 dark:bg-purple-950 dark:text-purple-100`;
const NEUTRAL_BANNER = `${BANNER} border-zinc-200 bg-zinc-50 text-zinc-800 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-200`;

/**
 * Whether the review still matches its pull request, checked on GitHub once the review has
 * finished and again whenever the window regains focus. An outdated review stays readable and
 * offers a review of the new head; a closed or merged pull request is named as such. Nothing is
 * shown while the review matches an open pull request's head.
 */
function FreshnessBanner({ run, finished }: { run: ReviewRun; finished: boolean }) {
  const router = useRouter();
  const freshness = useFreshness(run.id, finished);
  const request = useRequestReview();
  const busy = request.isPending || request.isSuccess;

  function reviewNewHead() {
    if (busy) return;
    request.mutate(
      {
        // Opens the new head's review if one was already requested.
        repositoryId: run.repository_id,
        pullRequestNumber: run.pull_request.number,
        mode: "reuse",
        idempotencyKey: crypto.randomUUID(),
      },
      { onSuccess: ({ run_id }) => router.push(`/reviews/${run_id}`) },
    );
  }

  if (!finished) return null;

  if (freshness.error) {
    // Access loss is shown for the whole page by the review query.
    if (isAccessLost(freshness.error)) return null;
    return (
      <section aria-label="Freshness" className={NEUTRAL_BANNER}>
        <p>
          <span className="font-medium">Freshness unknown:</span> could not check the pull
          request on GitHub. {reviewErrorMessage(freshness.error)}
        </p>
        <button
          type="button"
          onClick={() => void freshness.refetch()}
          disabled={freshness.isFetching}
          className="self-start font-medium underline disabled:opacity-50"
        >
          {freshness.isFetching ? "Checking…" : "Check again"}
        </button>
      </section>
    );
  }

  const current = freshness.data;
  if (!current) return null;
  const newHead = <Sha sha={current.current_head_sha} />;

  if (current.pull_request_state !== "open") {
    const merged = current.pull_request_state === "merged";
    return (
      <section aria-label="Freshness" className={merged ? MERGED_BANNER : NEUTRAL_BANNER}>
        <p>
          <span className="font-medium">{merged ? "Merged" : "Closed"}:</span>{" "}
          {merged
            ? "this pull request was merged."
            : "this pull request was closed without being merged."}{" "}
          {current.outdated ? (
            <>
              It had new commits after this review (head {newHead}). This review still shows the
              head it examined.
            </>
          ) : (
            "This review stays available."
          )}
        </p>
      </section>
    );
  }

  if (!current.outdated) return null;

  return (
    <section aria-label="Freshness" className={NOTICE_BANNER}>
      <p>
        <span className="font-medium">
          Outdated: the pull request has new commits (head {newHead}).
        </span>{" "}
        This review still shows the head it examined, <Sha sha={run.commits.head_sha} />.
      </p>
      <button
        type="button"
        onClick={reviewNewHead}
        disabled={busy}
        className="self-start rounded-md bg-zinc-900 px-4 py-1.5 font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
      >
        {busy ? "Requesting…" : "Review the new head"}
      </button>
      {request.error && (
        <p role="alert" className="text-red-700 dark:text-red-300">
          {reviewErrorMessage(request.error)}
        </p>
      )}
    </section>
  );
}

function RunProblem({ run }: { run: ReviewRun }) {
  const router = useRouter();
  const request = useRequestReview();
  const retryable = run.error?.retryable === true;
  const busy = request.isPending || request.isSuccess;

  function tryAgain() {
    if (busy) return;
    request.mutate(
      {
        // A new review of the same pull request, at its current head. A failed review is never
        // reused, so this opens a review of the head that is still waiting or running, if any.
        repositoryId: run.repository_id,
        pullRequestNumber: run.pull_request.number,
        mode: "reuse",
        idempotencyKey: crypto.randomUUID(),
      },
      { onSuccess: ({ run_id }) => router.push(`/reviews/${run_id}`) },
    );
  }

  return (
    <section
      aria-label="Problem"
      className="flex flex-col gap-2 rounded-md border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100"
    >
      <p className="font-medium">
        {run.status === "canceled"
          ? "This review was canceled."
          : "This review could not be completed."}
      </p>
      {run.error && <p>{run.error.message}</p>}
      {retryable && (
        <button
          type="button"
          onClick={tryAgain}
          disabled={busy}
          className="self-start rounded-md bg-zinc-900 px-4 py-1.5 font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
        >
          {busy ? "Requesting…" : "Try again"}
        </button>
      )}
      {request.error && (
        <p role="alert" className="text-red-700 dark:text-red-300">
          {reviewErrorMessage(request.error)}
        </p>
      )}
    </section>
  );
}

/** A set of open citation excerpts, by key. */
function useOpenSet() {
  const [open, setOpenKeys] = useState<ReadonlySet<string>>(() => new Set());
  const setOpen = useCallback((key: string, value: boolean) => {
    setOpenKeys((current) => {
      const next = new Set(current);
      if (value) next.add(key);
      else next.delete(key);
      return next;
    });
  }, []);
  return [open, setOpen] as const;
}

function plural(count: number, one: string, many: string) {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`;
}

/** The number of related code and test excerpts given to the review as context. */
function contextText(coverage: ReviewCoverage) {
  return plural(
    coverage.context_items,
    "related code or test excerpt",
    "related code and test excerpts",
  );
}

function omittedText(count: number) {
  return count === 1
    ? "1 item was removed because its citations could not be verified."
    : `${count.toLocaleString()} items were removed because their citations could not be verified.`;
}

function ReviewBody({
  run,
  review,
  coverage,
}: {
  run: ReviewRun;
  review: Review;
  coverage: ReviewCoverage;
}) {
  const [openLabels, setOpen] = useOpenSet();
  const citations = new Map(run.citations.map((citation) => [citation.label, citation]));
  const nothingToReview = run.quality_state === "nothing_to_review";
  const notReviewed = Math.max(0, coverage.changed_files - coverage.reviewed_files);
  const omitted = review.omitted_items;

  return (
    <>
      <section aria-labelledby="overview-heading" className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id="overview-heading" className="text-lg font-semibold">
            Overview
          </h2>
          <CopyMarkdown runId={run.id} />
        </div>
        {nothingToReview && (
          <div className="flex flex-col gap-1 rounded-md border border-zinc-200 bg-zinc-50 px-4 py-3 text-sm dark:border-zinc-800 dark:bg-zinc-900">
            <p className="font-medium">Nothing to review: no changed file could be reviewed.</p>
            <p className="text-zinc-600 dark:text-zinc-400">
              The coverage below lists each changed file and why it was not reviewed. This review
              did not count against the daily allowance.
            </p>
          </div>
        )}
        <div className="flex flex-col gap-2">
          <p className="flex items-center gap-2 text-sm font-medium">
            Overall risk
            <RiskLevelBadge level={review.overall_risk.level} />
          </p>
          {review.overall_risk.partial && (
            <p className="rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100">
              <span className="font-medium">
                Partial review: {plural(notReviewed, "file", "files")} not reviewed.
              </span>{" "}
              The risk level covers only the reviewed files. The coverage below lists the files
              that were left out and why.
            </p>
          )}
        </div>
        {review.overview && <p className="leading-relaxed whitespace-pre-wrap">{review.overview}</p>}
      </section>

      {review.summary.length > 0 && (
        <section aria-labelledby="summary-heading" className="flex flex-col gap-4">
          <h2 id="summary-heading" className="text-lg font-semibold">
            Summary
          </h2>
          <ul className="flex flex-col gap-6">
            {review.summary.map((area) => (
              <li key={area.area} className="flex flex-col gap-2">
                <h3 className="font-mono text-sm font-semibold break-all">{area.area}</h3>
                <ul className="flex flex-col gap-2">
                  {area.points.map((point, index) => (
                    <li
                      key={index}
                      className="flex flex-col gap-2 rounded-lg border border-zinc-200 px-4 py-3 dark:border-zinc-800"
                    >
                      <p className="flex items-start gap-3">
                        <ChangeChip change={point.change} />
                        <span className="min-w-0 break-words whitespace-pre-wrap">{point.text}</span>
                      </p>
                      <EvidenceLinks
                        labels={point.citations}
                        citations={citations}
                        onOpen={(label) => setOpen(label, true)}
                      />
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </section>
      )}

      {(!nothingToReview || review.risks.length > 0) && (
        <section aria-labelledby="risks-heading" className="flex flex-col gap-4">
          <h2 id="risks-heading" className="text-lg font-semibold">
            Risks
          </h2>
          {review.risks.length > 0 ? (
            <>
              <ul className="flex flex-col gap-3">
                {review.risks.map((risk) => (
                  <RiskItem key={risk.id} risk={risk} citations={citations} />
                ))}
              </ul>
              <p className="text-xs text-zinc-500">
                An observed risk is shown directly by the cited lines. A possible risk is only
                suggested by them.
              </p>
            </>
          ) : (
            <div className="flex flex-col gap-1 rounded-md border border-green-300 bg-green-50 px-4 py-3 text-sm text-green-950 dark:border-green-800 dark:bg-green-950 dark:text-green-100">
              <p className="font-medium">No risks found</p>
              <p>
                Examined {plural(coverage.reviewed_files, "changed file", "changed files")} of{" "}
                {coverage.changed_files.toLocaleString()} (
                {plural(coverage.changed_lines_reviewed, "changed line", "changed lines")}),
                with {contextText(coverage)} for context.
              </p>
            </div>
          )}
        </section>
      )}

      {!nothingToReview && <ChecklistSection items={review.checklist} />}

      {(!nothingToReview || review.tests.changed.length > 0) && (
        <TestsSection
          tests={review.tests}
          citations={citations}
          onOpen={(label) => setOpen(label, true)}
        />
      )}

      {omitted > 0 && (
        <p className="text-sm text-zinc-600 dark:text-zinc-400">{omittedText(omitted)}</p>
      )}

      <CoverageSection coverage={coverage} />

      {run.citations.length > 0 && (
        <section aria-labelledby="evidence-heading" className="flex flex-col gap-4">
          <h2 id="evidence-heading" className="text-lg font-semibold">
            Evidence
          </h2>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Every range the review cites, at the commit of its side of the change.
          </p>
          <ol className="flex flex-col gap-3">
            {run.citations.map((citation) => (
              <ReviewCitation
                key={citation.label}
                citation={citation}
                open={openLabels.has(citation.label)}
                onToggle={() => setOpen(citation.label, !openLabels.has(citation.label))}
                anchor
              />
            ))}
          </ol>
        </section>
      )}
    </>
  );
}

const CHANGE_STYLES: Record<FileChange, string> = {
  added: "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-200",
  modified: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-200",
  renamed: "bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-200",
  removed: "bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-200",
};

function ChangeChip({ change }: { change: FileChange }) {
  return (
    <span
      className={`mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-xs font-medium ${CHANGE_STYLES[change]}`}
    >
      {changeLabel(change)}
    </span>
  );
}

/** Links from a summary point to the evidence it cites, which open the cited excerpt. */
function EvidenceLinks({
  labels,
  citations,
  onOpen,
}: {
  labels: string[];
  citations: Map<string, Citation>;
  onOpen: (label: string) => void;
}) {
  if (labels.length === 0) return null;
  return (
    <p className="flex flex-wrap items-center gap-1.5 text-xs">
      <span className="text-zinc-500">Evidence:</span>
      {labels.map((label) => {
        const citation = citations.get(label);
        const side = citation?.side === "before" ? "Before" : "After";
        return citation ? (
          <a
            key={label}
            href={`#citation-${label}`}
            onClick={() => onOpen(label)}
            title={`${side}: ${citation.path}, ${citationLines(citation)}`}
            className="rounded bg-blue-50 px-1.5 py-0.5 font-mono font-medium text-blue-800 hover:underline dark:bg-blue-950 dark:text-blue-200"
          >
            {label}
          </a>
        ) : (
          <span key={label} className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono dark:bg-zinc-800">
            {label}
          </span>
        );
      })}
    </p>
  );
}

const CATEGORY_LABELS: Record<Risk["category"], string> = {
  correctness: "Correctness",
  security: "Security",
  data_and_migrations: "Data and migrations",
  compatibility: "Compatibility",
  performance: "Performance",
  dependencies: "Dependencies",
  tests: "Tests",
  other: "Other",
};

const BASIS_STYLES: Record<Risk["basis"], string> = {
  observed: "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-200",
  possible: "bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-200",
};

/** One risk: severity, category, basis, explanation, suggested check, and its citations. */
function RiskItem({ risk, citations }: { risk: Risk; citations: Map<string, Citation> }) {
  const [openLabels, setOpen] = useOpenSet();
  const cited = risk.citations.flatMap((label) => {
    const citation = citations.get(label);
    return citation ? [citation] : [];
  });

  return (
    <li
      id={`risk-${risk.id}`}
      className="flex scroll-mt-24 flex-col gap-3 rounded-lg border border-zinc-200 px-4 py-3 dark:border-zinc-800"
    >
      <div className="flex flex-col gap-2">
        <h3 className="flex items-start gap-2 font-medium">
          <span className="mt-0.5 shrink-0 font-mono text-xs text-zinc-500">{risk.id}</span>
          <span className="min-w-0 break-words">{risk.title}</span>
        </h3>
        <p className="flex flex-wrap items-center gap-2 text-xs">
          <RiskLevelBadge level={risk.severity} label="Severity" />
          <Label>{CATEGORY_LABELS[risk.category]}</Label>
          <span className={`rounded px-1.5 py-0.5 font-medium ${BASIS_STYLES[risk.basis]}`}>
            {risk.basis === "observed" ? "Observed" : "Possible"}
          </span>
          {risk.origin === "rule" && <Label>Found by a rule</Label>}
        </p>
      </div>
      {risk.path && (
        <p className="text-sm">
          <span className="text-zinc-500">File: </span>
          <code className="font-mono break-all">{risk.path}</code>
        </p>
      )}
      <p className="text-sm leading-relaxed break-words whitespace-pre-wrap">{risk.explanation}</p>
      {risk.suggested_check && (
        <div className="flex flex-col gap-0.5 text-sm">
          <p className="font-medium">Suggested check</p>
          <p className="break-words whitespace-pre-wrap text-zinc-700 dark:text-zinc-300">
            {risk.suggested_check}
          </p>
        </div>
      )}
      {cited.length > 0 && (
        <ol aria-label={`Citations for ${risk.id}`} className="flex flex-col gap-2">
          {cited.map((citation) => (
            <ReviewCitation
              key={citation.label}
              citation={citation}
              open={openLabels.has(citation.label)}
              onToggle={() => setOpen(citation.label, !openLabels.has(citation.label))}
              idPrefix={`risk-${risk.id}`}
            />
          ))}
        </ol>
      )}
    </li>
  );
}

type CopyState = "idle" | "copying" | "copied" | "failed";

/**
 * Copies the review as Markdown (FR-015). The copy is fetched as soon as the review is shown, so a
 * click usually writes it to the clipboard at once; otherwise it is fetched first.
 */
function CopyMarkdown({ runId }: { runId: string }) {
  const markdown = useReviewMarkdown(runId);
  const [state, setState] = useState<CopyState>("idle");
  const [problem, setProblem] = useState<string | null>(null);

  async function copy() {
    if (state === "copying") return;
    setState("copying");
    setProblem(null);
    let text = markdown.data;
    try {
      text ??= (await markdown.refetch({ throwOnError: true })).data;
    } catch (error) {
      setState("failed");
      setProblem(`Could not load the Markdown: ${errorMessage(error)}`);
      return;
    }
    try {
      if (text === undefined) throw new Error("No Markdown");
      await navigator.clipboard.writeText(text);
    } catch {
      setState("failed");
      setProblem("Could not write to the clipboard. Allow clipboard access and try again.");
      return;
    }
    setState("copied");
    window.setTimeout(() => setState((current) => (current === "copied" ? "idle" : current)), 2000);
  }

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <button
        type="button"
        onClick={() => void copy()}
        disabled={state === "copying"}
        className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm font-medium hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
      >
        {state === "copying" ? "Copying…" : "Copy as Markdown"}
      </button>
      <span role="status" className="text-sm text-green-700 dark:text-green-400">
        {state === "copied" ? "Copied" : ""}
      </span>
      {problem && (
        <p role="alert" className="w-full text-sm text-red-700 dark:text-red-400">
          {problem}
        </p>
      )}
    </div>
  );
}

/** What the reader should verify, each item with the changed files and risks it concerns. */
function ChecklistSection({ items }: { items: ChecklistItem[] }) {
  return (
    <section aria-labelledby="checklist-heading" className="flex flex-col gap-4">
      <h2 id="checklist-heading" className="text-lg font-semibold">
        Checklist
      </h2>
      {items.length === 0 ? (
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          No checklist items for this change.
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {items.map((item, index) => (
            <li
              key={index}
              className="flex items-start gap-3 rounded-lg border border-zinc-200 px-4 py-3 dark:border-zinc-800"
            >
              <span
                aria-hidden="true"
                className="mt-1 size-3.5 shrink-0 rounded-sm border border-zinc-400 dark:border-zinc-500"
              />
              <div className="flex min-w-0 flex-col gap-1.5">
                <p className="break-words whitespace-pre-wrap">{item.text}</p>
                {(item.paths.length > 0 || item.risk_ids.length > 0) && (
                  <p className="flex flex-wrap items-center gap-1.5 text-xs">
                    {item.paths.map((path) => (
                      <code
                        key={path}
                        className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono break-all dark:bg-zinc-800"
                      >
                        {path}
                      </code>
                    ))}
                    {item.risk_ids.map((id) => (
                      <a
                        key={id}
                        href={`#risk-${id}`}
                        className="rounded bg-amber-50 px-1.5 py-0.5 font-mono font-medium text-amber-900 hover:underline dark:bg-amber-950 dark:text-amber-100"
                      >
                        {id}
                      </a>
                    ))}
                  </p>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Text whose `backticked` parts, such as names in a candidate's reason, are shown as code. */
function CodeText({ text }: { text: string }) {
  const parts = text.split("`");
  // An unmatched backtick leaves an even number of parts: show the text as it is.
  if (parts.length % 2 === 0) return <>{text}</>;
  return (
    <>
      {parts.map((part, index) =>
        index % 2 === 1 ? (
          <code key={index} className="font-mono text-[0.9em]">
            {part}
          </code>
        ) : (
          <Fragment key={index}>{part}</Fragment>
        ),
      )}
    </>
  );
}

function TestGroup({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-2">
      <h3 className="font-medium">{title}</h3>
      {children}
    </div>
  );
}

function NoTests({ children }: { children: ReactNode }) {
  return <p className="text-sm text-zinc-600 dark:text-zinc-400">{children}</p>;
}

/**
 * The tests the pull request changes, existing tests found by name that may exercise it (never
 * run by CodeAtlas, FR-011 and FR-012), and new test cases the change appears to need.
 */
function TestsSection({
  tests,
  citations,
  onOpen,
}: {
  tests: ReviewTests;
  citations: Map<string, Citation>;
  onOpen: (label: string) => void;
}) {
  return (
    <section aria-labelledby="tests-heading" className="flex flex-col gap-6">
      <h2 id="tests-heading" className="text-lg font-semibold">
        Tests
      </h2>

      <TestGroup title="Changed in this pull request">
        {tests.changed.length === 0 ? (
          <NoTests>The pull request changes no tests.</NoTests>
        ) : (
          <ul className="flex flex-col gap-1.5 text-sm">
            {tests.changed.map((test) => (
              <li key={test.path} className="flex items-start gap-3">
                <ChangeChip change={test.change} />
                <code className="min-w-0 font-mono break-all">{test.path}</code>
              </li>
            ))}
          </ul>
        )}
      </TestGroup>

      <TestGroup title="Candidate tests (not run by CodeAtlas)">
        <p className="text-xs text-zinc-500">
          Existing tests found by name that may exercise the change. CodeAtlas has not run them,
          and the list may be incomplete.
        </p>
        {tests.candidates.length === 0 ? (
          <NoTests>No candidate tests were found.</NoTests>
        ) : (
          <ul className="flex flex-col gap-3">
            {tests.candidates.map((test) => (
              <CandidateTestItem key={test.path} test={test} citations={citations} />
            ))}
          </ul>
        )}
      </TestGroup>

      <TestGroup title="Suggested new tests">
        {tests.new_cases.length === 0 ? (
          <NoTests>No new tests are suggested.</NoTests>
        ) : (
          <ul className="flex flex-col gap-2">
            {tests.new_cases.map((testCase, index) => (
              <li
                key={index}
                className="flex flex-col gap-2 rounded-lg border border-zinc-200 px-4 py-3 dark:border-zinc-800"
              >
                <p className="break-words whitespace-pre-wrap">{testCase.behavior}</p>
                {testCase.location_hint && (
                  <p className="text-sm break-words text-zinc-600 dark:text-zinc-400">
                    Where: {testCase.location_hint}
                  </p>
                )}
                <EvidenceLinks labels={testCase.citations} citations={citations} onOpen={onOpen} />
              </li>
            ))}
          </ul>
        )}
      </TestGroup>
    </section>
  );
}

/** One candidate test: its path, why it was chosen, and its excerpt when the review has one. */
function CandidateTestItem({
  test,
  citations,
}: {
  test: CandidateTest;
  citations: Map<string, Citation>;
}) {
  const [openLabels, setOpen] = useOpenSet();
  const cited = test.citations.flatMap((label) => {
    const citation = citations.get(label);
    return citation ? [citation] : [];
  });

  return (
    <li className="flex flex-col gap-2 rounded-lg border border-zinc-200 px-4 py-3 dark:border-zinc-800">
      <p className="flex flex-col gap-0.5">
        <code className="font-mono text-sm font-medium break-all">{test.path}</code>
        <span className="text-sm text-zinc-600 dark:text-zinc-400">
          <CodeText text={test.reason.charAt(0).toUpperCase() + test.reason.slice(1)} />
        </span>
      </p>
      {cited.length > 0 && (
        <ol aria-label={`Excerpt of ${test.path}`} className="flex flex-col gap-2">
          {cited.map((citation) => (
            <ReviewCitation
              key={citation.label}
              citation={citation}
              open={openLabels.has(citation.label)}
              onToggle={() => setOpen(citation.label, !openLabels.has(citation.label))}
              idPrefix="candidate"
            />
          ))}
        </ol>
      )}
    </li>
  );
}

/** Why a changed file was not reviewed: 001's coverage reasons, plus the review limits. */
function coverageReason(file: CoverageFile) {
  if (file.reviewed) return "Reviewed";
  if (!file.reason) return "Not reviewed";
  if (file.reason === "review_limit") return "Over the review size limits";
  return reasonLabel(file.reason);
}

function LineCounts({ file }: { file: CoverageFile }) {
  if (file.additions == null && file.deletions == null) {
    return <span className="text-zinc-500">–</span>;
  }
  return (
    <span className="font-mono text-xs whitespace-nowrap tabular-nums">
      <span className="text-green-700 dark:text-green-400">+{file.additions ?? 0}</span>{" "}
      <span className="text-red-700 dark:text-red-400">−{file.deletions ?? 0}</span>
      <span className="sr-only">
        {" "}
        ({file.additions ?? 0} added, {file.deletions ?? 0} removed)
      </span>
    </span>
  );
}

/** Each changed file: whether it was reviewed, and why not (FR-017). */
function CoverageSection({ coverage }: { coverage: ReviewCoverage }) {
  return (
    <section aria-labelledby="coverage-heading" className="flex flex-col gap-4">
      <h2 id="coverage-heading" className="text-lg font-semibold">
        Coverage
      </h2>
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        Reviewed {plural(coverage.reviewed_files, "changed file", "changed files")} of{" "}
        {coverage.changed_files.toLocaleString()} ·{" "}
        {plural(coverage.changed_lines_reviewed, "changed line", "changed lines")} reviewed ·{" "}
        {contextText(coverage)} used as context
      </p>
      {coverage.files.length === 0 ? (
        <p className="text-sm text-zinc-600 dark:text-zinc-400">No files were changed.</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-zinc-200 dark:border-zinc-800">
          <table className="w-full text-left text-sm">
            <thead className="bg-zinc-50 text-xs text-zinc-600 dark:bg-zinc-900 dark:text-zinc-400">
              <tr>
                <th scope="col" className="px-3 py-2 font-medium">
                  Path
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Change
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Lines
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Result
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
              {coverage.files.map((file, index) => {
                const directory = file.entry_type === "directory";
                return (
                  <tr key={`${file.path}:${index}`}>
                    <td className="px-3 py-2 font-mono text-xs break-all">
                      {file.path}
                      {file.previous_path && (
                        <span className="block text-zinc-500">from {file.previous_path}</span>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      {directory
                        ? `Directory · ${plural(file.count ?? 0, "changed file", "changed files")}`
                        : file.change
                          ? changeLabel(file.change)
                          : "–"}
                    </td>
                    <td className="px-3 py-2">
                      <LineCounts file={file} />
                    </td>
                    <td
                      className={
                        file.reviewed
                          ? "px-3 py-2 text-green-700 dark:text-green-400"
                          : "px-3 py-2"
                      }
                    >
                      {coverageReason(file)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
