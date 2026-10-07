"use client";

import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";

import { AccessLostNotice } from "@/components/AccessLostNotice";
import { CodeLines } from "@/components/CodeView";
import { JobProgress } from "@/components/JobProgress";
import { RunStatusBadge } from "@/components/RunStatusBadge";
import { ApiError, errorMessage, isAccessLost } from "@/lib/api/client";
import {
  type Citation,
  type Claim,
  questionErrorMessage,
  type Run,
  useAskQuestion,
  useRun,
} from "@/lib/api/questions";
import { isActiveJob, shortSha, useRepository } from "@/lib/api/repositories";

type Answer = NonNullable<Run["answer"]>;

export function AnswerDetail({ runId }: { runId: string }) {
  const queryClient = useQueryClient();
  const { data: run, error } = useRun(runId);

  // When the answer job finishes, load the answer (or error) and refresh the history and usage.
  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["analysis-runs"] });
    void queryClient.invalidateQueries({ queryKey: ["usage"] });
  }, [queryClient]);

  // Checked before the cached answer, which stays in the cache after access is lost (FR-014).
  if (isAccessLost(error)) return <AccessLostNotice error={error} subject="This answer" />;

  if (!run) {
    if (error) {
      const missing = error instanceof ApiError && (error.status === 404 || error.status === 422);
      return (
        <div className="flex flex-col gap-4">
          <Link
            href="/repositories"
            className="text-sm text-zinc-600 hover:underline dark:text-zinc-400"
          >
            ← All repositories
          </Link>
          <p role="alert" className="text-sm text-red-700 dark:text-red-400">
            {missing
              ? "This answer does not exist or is no longer available."
              : `Could not load the answer: ${errorMessage(error)}`}
          </p>
        </div>
      );
    }
    return <p className="text-sm text-zinc-500">Loading answer…</p>;
  }

  // The same address space holds pull request reviews, which have their own page.
  if (run.kind !== "repository_qa") {
    return (
      <div className="flex flex-col gap-4">
        <RepositoryLink repositoryId={run.repository_id} />
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          This is a pull request review, not an answer.{" "}
          <Link href={`/reviews/${run.id}`} className="font-medium underline">
            Open the review
          </Link>
        </p>
      </div>
    );
  }

  const active = isActiveJob(run.status);

  return (
    <div className="flex flex-col gap-10">
      <header className="flex flex-col gap-3">
        <RepositoryLink repositoryId={run.repository_id} />
        <div className="flex flex-col gap-1">
          <p className="text-xs font-medium tracking-wide text-zinc-500 uppercase">Question</p>
          <h1 className="text-2xl font-semibold tracking-tight break-words whitespace-pre-wrap">
            {run.question}
          </h1>
        </div>
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-zinc-600 dark:text-zinc-400">
          <RunStatusBadge status={run.status} quality={run.quality_state} />
          <span>
            At commit{" "}
            <code title={run.commit_sha} className="font-medium text-zinc-900 dark:text-zinc-100">
              {shortSha(run.commit_sha)}
            </code>
          </span>
          <span aria-hidden="true">·</span>
          <span>
            Asked <time dateTime={run.created_at}>{new Date(run.created_at).toLocaleString()}</time>
          </span>
        </p>
      </header>

      {active &&
        (run.job_id ? (
          <JobProgress key={run.job_id} jobId={run.job_id} onFinished={refresh} />
        ) : (
          <p className="text-sm text-zinc-500">Waiting for the answer to start…</p>
        ))}

      {run.status === "succeeded" &&
        (run.answer ? (
          <AnswerBody run={run} answer={run.answer} />
        ) : (
          <p className="text-sm text-zinc-600 dark:text-zinc-400">The answer is not available.</p>
        ))}

      {(run.status === "failed" || run.status === "canceled") && <RunProblem run={run} />}
    </div>
  );
}

function RepositoryLink({ repositoryId }: { repositoryId: string }) {
  const { data: repository } = useRepository(repositoryId);
  return (
    <Link
      href={`/repositories/${repositoryId}`}
      className="self-start text-sm text-zinc-600 hover:underline dark:text-zinc-400"
    >
      ← {repository?.full_name ?? "Repository"}
    </Link>
  );
}

function AnswerBody({ run, answer }: { run: Run; answer: Answer }) {
  const [openLabels, setOpenLabels] = useState<ReadonlySet<string>>(() => new Set());
  const citations = new Map(run.citations.map((citation) => [citation.label, citation]));
  const insufficient = run.quality_state === "insufficient_evidence";

  function setOpen(label: string, open: boolean) {
    setOpenLabels((current) => {
      const next = new Set(current);
      if (open) next.add(label);
      else next.delete(label);
      return next;
    });
  }

  return (
    <>
      <section aria-labelledby="answer-heading" className="flex flex-col gap-4">
        <h2 id="answer-heading" className="text-lg font-semibold">
          {insufficient ? "Insufficient evidence" : "Answer"}
        </h2>
        {insufficient ? (
          <div className="flex flex-col gap-2 rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100">
            <p>{answer.summary}</p>
            {answer.gaps.length > 0 && (
              <>
                <p className="font-medium">What is missing:</p>
                <ul className="list-disc pl-5">
                  {answer.gaps.map((gap, index) => (
                    <li key={index}>{gap}</li>
                  ))}
                </ul>
              </>
            )}
          </div>
        ) : (
          <p className="leading-relaxed whitespace-pre-wrap">{answer.summary}</p>
        )}

        {answer.claims.length > 0 && (
          <>
            <ul className="flex flex-col gap-3">
              {answer.claims.map((claim, index) => (
                <li
                  key={index}
                  className="flex flex-col gap-2 rounded-lg border border-zinc-200 px-4 py-3 dark:border-zinc-800"
                >
                  <ClaimRow
                    claim={claim}
                    citations={citations}
                    onOpen={(label) => setOpen(label, true)}
                  />
                </li>
              ))}
            </ul>
            <p className="text-xs text-zinc-500">
              A fact is stated directly by the cited lines. An inference is a likely explanation
              drawn from them.
            </p>
          </>
        )}

        {!insufficient && answer.gaps.length > 0 && (
          <div className="flex flex-col gap-1 text-sm">
            <p className="font-medium">Not covered by the evidence:</p>
            <ul className="list-disc pl-5 text-zinc-700 dark:text-zinc-300">
              {answer.gaps.map((gap, index) => (
                <li key={index}>{gap}</li>
              ))}
            </ul>
          </div>
        )}
      </section>

      {run.citations.length > 0 && (
        <section aria-labelledby="citations-heading" className="flex flex-col gap-4">
          <h2 id="citations-heading" className="text-lg font-semibold">
            Citations
          </h2>
          <ol className="flex flex-col gap-3">
            {run.citations.map((citation) => (
              <CitationItem
                key={citation.label}
                citation={citation}
                open={openLabels.has(citation.label)}
                onToggle={() => setOpen(citation.label, !openLabels.has(citation.label))}
              />
            ))}
          </ol>
        </section>
      )}
    </>
  );
}

const KIND_STYLES: Record<Claim["kind"], string> = {
  fact: "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-200",
  inference: "bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-200",
};

function ClaimRow({
  claim,
  citations,
  onOpen,
}: {
  claim: Claim;
  citations: Map<string, Citation>;
  onOpen: (label: string) => void;
}) {
  return (
    <>
      <p className="flex items-start gap-3">
        <span
          className={`mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-xs font-medium ${KIND_STYLES[claim.kind]}`}
        >
          {claim.kind === "fact" ? "Fact" : "Inference"}
        </span>
        <span className="min-w-0 break-words">{claim.text}</span>
      </p>
      {claim.citations.length > 0 && (
        <p className="flex flex-wrap items-center gap-1.5 text-xs">
          <span className="text-zinc-500">Evidence:</span>
          {claim.citations.map((label) => {
            const citation = citations.get(label);
            return citation ? (
              <a
                key={label}
                href={`#citation-${label}`}
                onClick={() => onOpen(label)}
                title={`${citation.path}, ${lineText(citation)}`}
                className="rounded bg-blue-50 px-1.5 py-0.5 font-mono font-medium text-blue-800 hover:underline dark:bg-blue-950 dark:text-blue-200"
              >
                {label}
              </a>
            ) : (
              <span
                key={label}
                className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono dark:bg-zinc-800"
              >
                {label}
              </span>
            );
          })}
        </p>
      )}
    </>
  );
}

function lineText({ start_line, end_line }: Citation) {
  return start_line === end_line ? `line ${start_line}` : `lines ${start_line}–${end_line}`;
}

/** The excerpt's lines, without the empty line after a final line break. */
function excerptLines(excerpt: string) {
  const lines = excerpt.split("\n");
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

function CitationItem({
  citation,
  open,
  onToggle,
}: {
  citation: Citation;
  open: boolean;
  onToggle: () => void;
}) {
  const excerptId = `citation-${citation.label}-excerpt`;
  return (
    <li
      id={`citation-${citation.label}`}
      tabIndex={-1}
      className="scroll-mt-24 overflow-hidden rounded-lg border border-zinc-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-zinc-800"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3">
        <span className="rounded bg-blue-50 px-1.5 py-0.5 font-mono text-xs font-medium text-blue-800 dark:bg-blue-950 dark:text-blue-200">
          {citation.label}
        </span>
        <span className="min-w-0 font-mono text-sm break-all">{citation.path}</span>
        <span className="text-xs text-zinc-500">
          {lineText(citation)} · commit{" "}
          <code title={citation.commit_sha}>{shortSha(citation.commit_sha)}</code>
        </span>
        <span className="ml-auto flex items-center gap-4 text-sm">
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={open}
            aria-controls={excerptId}
            className="font-medium underline"
          >
            {open ? "Hide excerpt" : "Show excerpt"}
          </button>
          <Link href={citation.view_url} className="font-medium underline">
            Open in file browser
          </Link>
        </span>
      </div>
      <div
        id={excerptId}
        hidden={!open}
        className="border-t border-zinc-200 py-2 dark:border-zinc-800"
      >
        <CodeLines lines={excerptLines(citation.excerpt)} firstLine={citation.start_line} />
      </div>
    </li>
  );
}

function RunProblem({ run }: { run: Run }) {
  const router = useRouter();
  const ask = useAskQuestion();
  const retryable = run.error?.retryable === true;
  const busy = ask.isPending || ask.isSuccess;

  function askAgain() {
    if (busy) return;
    ask.mutate(
      {
        // The same question at the same indexed version.
        body: {
          repository_id: run.repository_id,
          kind: "repository_qa",
          question: run.question,
          target: { snapshot_id: run.snapshot_id },
        },
        idempotencyKey: crypto.randomUUID(),
      },
      { onSuccess: ({ run_id }) => router.push(`/answers/${run_id}`) },
    );
  }

  return (
    <section
      aria-label="Problem"
      className="flex flex-col gap-2 rounded-md border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100"
    >
      <p className="font-medium">
        {run.status === "canceled"
          ? "This question was canceled."
          : "This question could not be answered."}
      </p>
      {run.error && <p>{run.error.message}</p>}
      {retryable && (
        <button
          type="button"
          onClick={askAgain}
          disabled={busy}
          className="self-start rounded-md bg-zinc-900 px-4 py-1.5 font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
        >
          {busy ? "Asking…" : "Ask again"}
        </button>
      )}
      {ask.error && (
        <p role="alert" className="text-red-700 dark:text-red-300">
          {questionErrorMessage(ask.error)}
        </p>
      )}
    </section>
  );
}
