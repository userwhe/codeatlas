"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { type FormEvent, type ReactNode, useCallback, useState } from "react";

import { AnswerHistory } from "@/components/AnswerHistory";
import { AskQuestionForm } from "@/components/AskQuestionForm";
import { AutomaticUpdatesStatus } from "@/components/AutomaticUpdatesStatus";
import { CoverageTable } from "@/components/CoverageTable";
import { DisconnectRepositoryDialog } from "@/components/DisconnectRepositoryDialog";
import { ExternalProcessingAcceptance } from "@/components/ExternalProcessingAcceptance";
import { JobProgress } from "@/components/JobProgress";
import { PullRequestList } from "@/components/PullRequestList";
import { RepositoryAccessPanel } from "@/components/RepositoryAccessPanel";
import { RepositoryStateBadge } from "@/components/RepositoryStateBadge";
import { SnapshotSelector } from "@/components/SnapshotSelector";
import { api, ApiError, errorMessage, unwrap } from "@/lib/api/client";
import { useMe } from "@/lib/api/me";
import {
  type ActiveSnapshot,
  indexingStatusLabel,
  isActiveJob,
  isPausedForDisclosure,
  type JobError,
  type LatestJob,
  type Repository,
  type RepositoryState,
  shortSha,
  triggerLabel,
  useRepository,
  useSnapshot,
} from "@/lib/api/repositories";

export function RepositoryDetail({ repositoryId }: { repositoryId: string }) {
  const queryClient = useQueryClient();
  const { data: repository, error } = useRepository(repositoryId, { followAccessChecks: true });
  // The job whose progress stays on screen once it is no longer the active job: the last one
  // that finished while this page was open, or the one a re-index just started.
  const [followedJobId, setFollowedJobId] = useState<string | null>(null);
  // The job of a re-index requested here while access was lost; its outcome is shown if it ends
  // with access still lost.
  const [accessCheckJobId, setAccessCheckJobId] = useState<string | null>(null);
  const [disconnecting, setDisconnecting] = useState(false);

  const follow = useCallback(
    (jobId: string) => {
      setFollowedJobId(jobId);
      void queryClient.invalidateQueries({ queryKey: ["repositories"] });
      void queryClient.invalidateQueries({ queryKey: ["snapshots"] });
    },
    [queryClient],
  );

  if (!repository) {
    if (error) {
      const missing = error instanceof ApiError && (error.status === 404 || error.status === 422);
      return (
        <div className="flex flex-col gap-4">
          <BackLink />
          <p role="alert" className="text-sm text-red-700 dark:text-red-400">
            {missing
              ? "This repository does not exist or is not connected to your workspace."
              : `Could not load the repository: ${errorMessage(error)}`}
          </p>
        </div>
      );
    }
    return <p className="text-sm text-zinc-500">Loading repository…</p>;
  }

  const latestJob = repository.latest_indexing_job;
  const progressJobId = latestJob && isActiveJob(latestJob.status) ? latestJob.id : followedJobId;
  const failure = latestJob?.status === "failed" ? latestJob.error : null;
  const snapshot = repository.active_snapshot;
  // While access is lost, every read of the repository's content and of its jobs is denied
  // (FR-014): only what the repository itself reports is shown.
  const lost = repository.state === "access_lost";

  function reindexStarted(jobId: string) {
    if (lost) setAccessCheckJobId(jobId);
    follow(jobId);
  }

  return (
    <div className="flex flex-col gap-10">
      <header className="flex flex-col gap-2">
        <BackLink />
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight break-all">
            {repository.full_name}
          </h1>
          <RepositoryStateBadge state={repository.state} />
        </div>
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          {repository.private ? "Private" : "Public"} · default branch {repository.default_branch}
        </p>
      </header>

      <Section title="GitHub access">
        <RepositoryAccessPanel access={repository.access} />
      </Section>

      <Section title="Indexing">
        {latestJob && <LatestRun job={latestJob} />}
        {lost ? (
          <AccessCheck job={latestJob} requestedJobId={accessCheckJobId} />
        ) : (
          <>
            {progressJobId && (
              <JobProgress
                key={progressJobId}
                jobId={progressJobId}
                onFinished={() => follow(progressJobId)}
              />
            )}
            {failure && <IndexingFailure state={repository.state} error={failure} />}
          </>
        )}
        <ReindexForm repository={repository} onStarted={reindexStarted} />
      </Section>

      <Section title="Automatic updates">
        <AutomaticUpdatesStatus
          automaticUpdates={repository.automatic_updates}
          latestPush={repository.latest_push}
          defaultCommitSha={snapshot?.commit_sha ?? null}
        />
      </Section>

      <Section title="Indexed version">
        {snapshot ? (
          lost ? (
            <SnapshotFields snapshot={snapshot} />
          ) : (
            <SnapshotSummary snapshot={snapshot} />
          )
        ) : (
          <p className="text-sm text-zinc-600 dark:text-zinc-400">No indexed version yet.</p>
        )}
        {lost ? (
          <HiddenWhileLost>Browsing and searching indexed versions</HiddenWhileLost>
        ) : (
          <SnapshotSelector repositoryId={repository.id} />
        )}
      </Section>

      <Section title="Questions">
        {lost ? (
          <HiddenWhileLost>Asking questions and reading earlier answers</HiddenWhileLost>
        ) : (
          <>
            <AskQuestionForm repositoryId={repository.id} hasReadyVersion={snapshot !== null} />
            <AnswerHistory repositoryId={repository.id} />
          </>
        )}
      </Section>

      <Section title="Pull requests">
        {lost ? (
          <HiddenWhileLost>Reviewing pull requests</HiddenWhileLost>
        ) : (
          <PullRequestList repositoryId={repository.id} />
        )}
      </Section>

      {snapshot && !lost && (
        <Section title="Skipped entries">
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Files and directories at this commit that were not indexed, or were indexed only as
            text, and why.
          </p>
          <CoverageTable snapshotId={snapshot.id} />
        </Section>
      )}

      <Section title="Disconnect">
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          Disconnecting removes this repository and all of its indexed data, questions, and
          answers from CodeAtlas. It does not change anything on GitHub.
        </p>
        <button
          type="button"
          onClick={() => setDisconnecting(true)}
          className="self-start rounded-md border border-red-300 px-4 py-1.5 text-sm font-medium text-red-700 hover:bg-red-50 dark:border-red-800 dark:text-red-400 dark:hover:bg-red-950"
        >
          Disconnect repository
        </button>
        {disconnecting && (
          <DisconnectRepositoryDialog
            repository={repository}
            onClose={() => setDisconnecting(false)}
          />
        )}
      </Section>
    </div>
  );
}

function BackLink() {
  return (
    <Link href="/repositories" className="text-sm text-zinc-600 hover:underline dark:text-zinc-400">
      ← All repositories
    </Link>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  const id = `section-${title.toLowerCase().replaceAll(" ", "-")}`;
  return (
    <section aria-labelledby={id} className="flex flex-col gap-4">
      <h2 id={id} className="text-lg font-semibold">
        {title}
      </h2>
      {children}
    </section>
  );
}

function HiddenWhileLost({ children }: { children: ReactNode }) {
  return (
    <p className="text-sm text-zinc-600 dark:text-zinc-400">
      {children} are unavailable while access to the repository is lost.
    </p>
  );
}

/**
 * The indexing run of a repository whose access is lost. Its progress cannot be read (FR-014),
 * so the page polls the repository instead: while the run is active, it is checking access. If
 * it restores access, the page leaves this state and shows the run's progress. If a re-index
 * requested here ends with access still lost, the run's error says why.
 */
function AccessCheck({
  job,
  requestedJobId,
}: {
  job: LatestJob | null;
  requestedJobId: string | null;
}) {
  const active = job !== null && isActiveJob(job.status);
  const failed = job !== null && job.id === requestedJobId && !active && job.status !== "succeeded";

  return (
    <>
      {active && (
        <p role="status" className="text-sm font-medium">
          Checking access…
        </p>
      )}
      {failed && (
        <div className="flex flex-col gap-1 rounded-md border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100">
          <p className="font-medium">Access is still lost.</p>
          <p>{job.error?.message ?? "The run ended without restoring access."}</p>
        </div>
      )}
    </>
  );
}

/** What started the latest indexing run, and its state. */
function LatestRun({ job }: { job: LatestJob }) {
  return (
    <p className="text-sm text-zinc-600 dark:text-zinc-400">
      Latest run:{" "}
      <span className="font-medium text-foreground">{triggerLabel(job.trigger)}</span> ·{" "}
      {indexingStatusLabel(job.status)}
    </p>
  );
}

const COUNTS: [key: string, label: string][] = [
  ["indexed_files", "Indexed files"],
  ["source_lines", "Source lines"],
  ["symbols", "Symbols"],
  ["doc_chunks", "Documentation passages"],
];

/** The commit, branch, and time of a version, as the repository reports them. */
function SnapshotFields({ snapshot }: { snapshot: ActiveSnapshot }) {
  return (
    <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-3">
      <Field label="Commit">
        <code title={snapshot.commit_sha}>{shortSha(snapshot.commit_sha)}</code>
      </Field>
      <Field label="Branch">{snapshot.branch}</Field>
      <Field label="Ready at">
        {snapshot.ready_at ? new Date(snapshot.ready_at).toLocaleString() : "Unknown"}
      </Field>
    </dl>
  );
}

function SnapshotSummary({ snapshot }: { snapshot: ActiveSnapshot }) {
  const detail = useSnapshot(snapshot.id);
  const coverage = detail.data?.coverage;
  const count = (key: string) => {
    const value = coverage?.[key];
    return typeof value === "number" ? value : null;
  };

  return (
    <div className="flex flex-col gap-4">
      <SnapshotFields snapshot={snapshot} />
      {detail.data ? (
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {COUNTS.map(([key, label]) => (
            <div
              key={key}
              className="rounded-lg border border-zinc-200 px-4 py-3 dark:border-zinc-800"
            >
              <dt className="text-xs text-zinc-500">{label}</dt>
              <dd className="text-xl font-semibold tabular-nums">
                {count(key)?.toLocaleString() ?? "–"}
              </dd>
            </div>
          ))}
        </dl>
      ) : detail.error ? (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          Could not load the coverage counts: {errorMessage(detail.error)}
        </p>
      ) : (
        <p className="text-sm text-zinc-500">Loading coverage counts…</p>
      )}
      {count("symbols") === 0 && (
        <p className="rounded-md border border-zinc-200 bg-zinc-50 px-4 py-3 text-sm dark:border-zinc-800 dark:bg-zinc-900">
          No code structure was extracted. Declarations are extracted from Python and TypeScript
          files; other files are searchable as text.
        </p>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-zinc-500">{label}</dt>
      <dd className="font-medium">{children}</dd>
    </div>
  );
}

const MIB = 1024 * 1024;

function formatBytes(bytes: number) {
  if (bytes >= 1024 * MIB) return `${Number((bytes / (1024 * MIB)).toFixed(1))} GiB`;
  if (bytes >= MIB) return `${Number((bytes / MIB).toFixed(1))} MiB`;
  return `${bytes.toLocaleString()} bytes`;
}

// Plain-language names for the limits that `limit_exceeded` reports (FR-009).
const LIMITS: Record<string, (value: number) => string> = {
  max_files_per_snapshot: (value) => `${value.toLocaleString()} eligible files`,
  max_source_lines_per_snapshot: (value) => `${value.toLocaleString()} source lines`,
  max_expanded_bytes: (value) => `${formatBytes(value)} of source`,
  max_archive_bytes: (value) => `${formatBytes(value)} of downloaded source`,
  max_archive_members: (value) => `${value.toLocaleString()} entries in the source archive`,
};

/** The limit named in a `limit_exceeded` message, or the message itself if it cannot be read. */
function limitText(message: string) {
  const match = /limit (\w+) \((\d+)\)/.exec(message);
  const describe = match ? LIMITS[match[1]] : undefined;
  if (!match || !describe) return message;
  return `The repository is over the limit of ${describe(Number(match[2]))} per indexed version, so it was not indexed.`;
}

function failureText(error: JobError) {
  switch (error.code) {
    case "limit_exceeded":
      return limitText(error.message);
    case "branch_not_found":
      return `${error.message} Re-index with an existing branch, or leave the branch empty to use the default branch.`;
    case "repository_empty":
      return "The repository has no commits yet, so there is nothing to index. Push a commit, then re-index.";
    case "access_denied":
      return "CodeAtlas can no longer read this repository on GitHub. Make sure the CodeAtlas GitHub App is installed with access to it, then re-index.";
    default:
      return error.message;
  }
}

function IndexingFailure({ state, error }: { state: RepositoryState; error: JobError }) {
  const { data: me } = useMe();
  const title =
    state === "rejected"
      ? "Indexing was rejected"
      : state === "ready"
        ? "The latest indexing attempt failed. The indexed version below is still in use."
        : "Indexing failed";
  const tone =
    state === "rejected"
      ? "border-amber-300 bg-amber-50 text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100"
      : "border-red-300 bg-red-50 text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100";

  return (
    <div className={`flex flex-col gap-1 rounded-md border px-4 py-3 text-sm ${tone}`}>
      <p className="font-medium">{title}</p>
      <p>{failureText(error)}</p>
      {error.code === "access_denied" && me && (
        <a
          href={me.github_app_install_url}
          target="_blank"
          rel="noreferrer"
          className="self-start font-medium underline"
        >
          Install the GitHub App
        </a>
      )}
    </div>
  );
}

interface ReindexRequest {
  branch?: string;
  accept_external_processing: boolean;
}

function ReindexForm({
  repository,
  onStarted,
}: {
  repository: Repository;
  onStarted: (jobId: string) => void;
}) {
  const queryClient = useQueryClient();
  const [branch, setBranch] = useState("");
  const [accepted, setAccepted] = useState(false);
  // A repository that became private on GitHub is re-indexed only once the disclosure is
  // accepted; accepting also resumes its automatic updates (FR-017).
  const needsAcceptance = isPausedForDisclosure(repository.automatic_updates);
  const reindex = useMutation({
    mutationFn: ({ body, idempotencyKey }: { body: ReindexRequest; idempotencyKey: string }) =>
      unwrap(
        api.POST("/v1/repositories/{repository_id}/index", {
          params: {
            path: { repository_id: repository.id },
            header: { "Idempotency-Key": idempotencyKey },
          },
          body,
        }),
      ),
    onSuccess: ({ job }) => {
      setAccepted(false);
      onStarted(job.id);
    },
    onError: (error) => {
      // The repository was paused after this page loaded: reload it to show the checkbox.
      if (error instanceof ApiError && error.code === "external_processing_not_accepted") {
        void queryClient.invalidateQueries({ queryKey: ["repositories", repository.id] });
      }
    },
  });
  const blocked = needsAcceptance && !accepted;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (reindex.isPending || blocked) return;
    const trimmedBranch = branch.trim();
    reindex.mutate({
      body: {
        ...(trimmedBranch ? { branch: trimmedBranch } : {}),
        accept_external_processing: needsAcceptance && accepted,
      },
      // A new key for each submission attempt; a replay of the same request returns the same job.
      idempotencyKey: crypto.randomUUID(),
    });
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-2">
      {needsAcceptance && (
        <ExternalProcessingAcceptance checked={accepted} onChange={setAccepted} />
      )}
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <label htmlFor="reindex-branch" className="text-sm font-medium">
            Branch (optional)
          </label>
          <input
            id="reindex-branch"
            value={branch}
            onChange={(event) => setBranch(event.target.value)}
            placeholder={repository.default_branch}
            maxLength={255}
            className="rounded-md border border-zinc-300 bg-transparent px-3 py-1.5 text-sm dark:border-zinc-700"
          />
        </div>
        <button
          type="submit"
          disabled={reindex.isPending || blocked}
          className="rounded-md border border-zinc-300 px-4 py-1.5 text-sm font-medium hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
        >
          {reindex.isPending ? "Starting…" : "Re-index"}
        </button>
      </div>
      <p className="text-xs text-zinc-500">
        Indexes the latest commit of the branch. Leave the branch empty to use the default branch.
      </p>
      {reindex.error && (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          {errorMessage(reindex.error)}
        </p>
      )}
    </form>
  );
}
