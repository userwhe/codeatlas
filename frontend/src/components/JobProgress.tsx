"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { api, ApiError, unwrap } from "@/lib/api/client";
import type { paths } from "@/lib/api/schema";

type EventsPage =
  paths["/v1/jobs/{job_id}/events"]["get"]["responses"][200]["content"]["application/json"];
type Job = paths["/v1/jobs/{job_id}"]["get"]["responses"][200]["content"]["application/json"];
type JobEvent = EventsPage["items"][number];

export type TerminalStatus = "succeeded" | "failed" | "canceled";

const TERMINAL_STATUSES: readonly string[] = ["succeeded", "failed", "canceled"];
const POLL_INTERVAL_MS = 1500;

function isTerminal(status: string | undefined): status is TerminalStatus {
  return status !== undefined && TERMINAL_STATUSES.includes(status);
}

/** Everything received so far for one job; kept in the query cache between polls. */
interface Progress {
  status: EventsPage["job_status"];
  events: JobEvent[];
  nextAfter: number;
  job: Job | null;
}

/**
 * Fetches the events after the last seen `seq` and appends them to the cached progress. With no
 * cached progress (first mount or after a reload) it starts from `after=0` and rebuilds the full
 * list; after a remount it resumes from the cached cursor (research R4, FR-026). The job itself is
 * fetched only while queued (for `queued_behind`) and once finished (for the error).
 */
async function fetchProgress(
  jobId: string,
  previous: Progress | undefined,
): Promise<Progress> {
  const events = [...(previous?.events ?? [])];
  let after = previous?.nextAfter ?? 0;
  let page: EventsPage;
  do {
    page = await unwrap(
      api.GET("/v1/jobs/{job_id}/events", {
        params: { path: { job_id: jobId }, query: { after } },
      }),
    );
    const cursor = after;
    events.push(...page.items.filter((event) => event.seq > cursor));
    after = page.next_after;
    // Pages are capped, so once polling is about to stop, read until nothing is left.
  } while (isTerminal(page.job_status) && page.items.length > 0);

  const status = page.job_status;
  const needJob = status === "queued" || isTerminal(status);
  const job = needJob
    ? await unwrap(api.GET("/v1/jobs/{job_id}", { params: { path: { job_id: jobId } } }))
    : null;
  return { status, events, nextAfter: after, job };
}

/** One row per stage, in the order stages started, with the latest message for each. */
function stagesOf(events: JobEvent[]) {
  const stages = new Map<string, { message: JobEvent["message"]; done: boolean }>();
  for (const event of events) {
    if (!event.stage) continue;
    stages.set(event.stage, {
      message: event.message,
      done: event.event_type === "stage_completed",
    });
  }
  return [...stages].map(([stage, state]) => ({ stage, ...state }));
}

function stageLabel(stage: string) {
  const words = stage.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function statusText(progress: Progress) {
  switch (progress.status) {
    case "queued": {
      const ahead = progress.job?.queued_behind ?? 0;
      if (ahead > 0) {
        return `Queued: waiting for ${ahead} ${ahead === 1 ? "job" : "jobs"} ahead of this one.`;
      }
      return "Queued: starting soon.";
    }
    case "running":
      return "In progress.";
    case "retry_wait":
      return "A temporary problem occurred. Retrying shortly.";
    case "succeeded":
      return "Completed.";
    case "failed":
      return `Failed: ${progress.job?.error?.message ?? "the job could not be completed."}`;
    case "canceled":
      return "Canceled.";
    default:
      return progress.status;
  }
}

export function JobProgress({
  jobId,
  onFinished,
}: {
  jobId: string;
  onFinished?: (status: TerminalStatus) => void;
}) {
  const queryClient = useQueryClient();
  const { data: progress, error } = useQuery({
    queryKey: ["jobs", jobId, "progress"],
    queryFn: ({ queryKey }) =>
      fetchProgress(jobId, queryClient.getQueryData<Progress>(queryKey)),
    refetchInterval: (query) => {
      if (isTerminal(query.state.data?.status)) return false;
      if (query.state.error instanceof ApiError && query.state.error.status < 500) return false;
      return POLL_INTERVAL_MS;
    },
    staleTime: (query) => (isTerminal(query.state.data?.status) ? Infinity : 0),
  });

  const status = progress?.status;
  const reportedFor = useRef<string | null>(null);
  useEffect(() => {
    if (isTerminal(status) && reportedFor.current !== jobId) {
      reportedFor.current = jobId;
      onFinished?.(status);
    }
  }, [jobId, status, onFinished]);

  if (!progress) {
    if (error) {
      return (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          Could not load progress: {error.message}
        </p>
      );
    }
    return <p className="text-sm text-zinc-500">Loading progress…</p>;
  }

  const stages = stagesOf(progress.events);
  const finished = isTerminal(progress.status);

  return (
    <section aria-label="Job progress" className="flex flex-col gap-3">
      <p
        aria-live="polite"
        className={
          progress.status === "failed"
            ? "text-sm font-medium text-red-700 dark:text-red-400"
            : "text-sm font-medium"
        }
      >
        {statusText(progress)}
      </p>
      {stages.length > 0 && (
        <ol className="flex flex-col gap-1.5 text-sm">
          {stages.map(({ stage, message, done }) => (
            <li key={stage} className="flex items-baseline gap-2">
              <span
                aria-hidden="true"
                className={done ? "text-green-600 dark:text-green-400" : "text-zinc-400"}
              >
                {done ? "✓" : "•"}
              </span>
              <span className="font-medium">{stageLabel(stage)}</span>
              <span className="text-zinc-600 dark:text-zinc-400">{message}</span>
              <span className="sr-only">
                {done ? "(done)" : finished ? "(stopped)" : "(in progress)"}
              </span>
            </li>
          ))}
        </ol>
      )}
      {error && !finished && (
        <p role="alert" className="text-sm text-amber-700 dark:text-amber-400">
          Progress updates are delayed: {error.message}
        </p>
      )}
    </section>
  );
}
