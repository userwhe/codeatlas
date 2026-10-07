"use client";

import type { ReactNode } from "react";

import { EXTERNAL_PROCESSING_DISCLOSURE } from "@/components/ExternalProcessingAcceptance";
import { RelativeTime } from "@/components/RelativeTime";
import {
  type AutomaticUpdates,
  indexingStatusLabel,
  type LatestPush,
  shortSha,
} from "@/lib/api/repositories";

/**
 * Automatic re-indexing after pushes to the default branch: whether it is on or paused and how
 * to resume it (FR-016, FR-017), the latest push received, and the state of the run that covers
 * it (FR-009). When that run failed, it says why and that the previous version is still the
 * default (US1-6).
 */
export function AutomaticUpdatesStatus({
  automaticUpdates,
  latestPush,
  defaultCommitSha,
}: {
  automaticUpdates: AutomaticUpdates;
  latestPush: LatestPush | null;
  /** The commit of the repository's default (active) version, if it has one. */
  defaultCommitSha: string | null;
}) {
  return (
    <div className="flex flex-col gap-3 text-sm">
      {automaticUpdates.state === "paused" ? (
        <PausedNotice reason={automaticUpdates.reason} />
      ) : (
        <p className="text-zinc-600 dark:text-zinc-400">
          New commits pushed to the default branch are indexed automatically.
        </p>
      )}
      {latestPush ? (
        <LatestPushDetails latestPush={latestPush} defaultCommitSha={defaultCommitSha} />
      ) : (
        <p className="text-zinc-600 dark:text-zinc-400">No pushes received yet.</p>
      )}
    </div>
  );
}

/** Why automatic updates are paused, and what resumes them (research R8). */
function PausedNotice({ reason }: { reason: string | null }) {
  return (
    <div className="flex flex-col gap-2 rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100">
      {reason === "sign_in_required" ? (
        <>
          <p className="font-medium">Automatic updates paused. Sign in again to resume.</p>
          <p>
            Your GitHub authorization for CodeAtlas expired or was revoked, so new pushes are
            recorded but not indexed. When you sign in again, CodeAtlas checks access to the
            repository right away and resumes automatic updates.
          </p>
          {/* A plain link: the browser must do a full navigation to the API's redirect. */}
          <a href="/auth/github/login" className="self-start font-medium underline">
            Sign in with GitHub
          </a>
        </>
      ) : reason === "external_processing_not_accepted" ? (
        <>
          <p className="font-medium">Automatic updates paused.</p>
          <p>
            This repository became private on GitHub, so new pushes are recorded but not indexed.
            Automatic updates resume after you accept this for the private repository:
          </p>
          <p className="font-medium">{EXTERNAL_PROCESSING_DISCLOSURE}.</p>
          <p>To accept it, check the box in the Indexing section above, then choose Re-index.</p>
        </>
      ) : (
        <>
          <p className="font-medium">Automatic updates paused.</p>
          <p>New pushes are recorded but not indexed.</p>
        </>
      )}
    </div>
  );
}

function LatestPushDetails({
  latestPush,
  defaultCommitSha,
}: {
  latestPush: LatestPush;
  defaultCommitSha: string | null;
}) {
  const { job } = latestPush;

  return (
    <>
      <dl className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Field label="Latest push">
          <code title={latestPush.commit_sha}>{shortSha(latestPush.commit_sha)}</code>
        </Field>
        <Field label="Received">
          <RelativeTime value={latestPush.received_at} />
        </Field>
        <Field label="Run">{job ? indexingStatusLabel(job.status) : "Not started"}</Field>
      </dl>
      {job?.status === "failed" && (
        <div className="flex flex-col gap-1">
          <p className="font-medium text-red-700 dark:text-red-400">This push was not indexed.</p>
          {job.error && <p>{job.error.message}</p>}
          <p className="text-zinc-600 dark:text-zinc-400">
            {defaultCommitSha ? (
              <>
                The previous version,{" "}
                <code title={defaultCommitSha}>{shortSha(defaultCommitSha)}</code>, is still the
                default.
              </>
            ) : (
              "There is no indexed version yet."
            )}
          </p>
        </div>
      )}
      {!job && (
        <p className="text-zinc-600 dark:text-zinc-400">
          This push was not indexed because automatic updates were paused when it arrived.
        </p>
      )}
    </>
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
