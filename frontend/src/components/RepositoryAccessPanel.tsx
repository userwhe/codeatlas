"use client";

import type { ReactNode } from "react";

import { RelativeTime } from "@/components/RelativeTime";
import { useMe } from "@/lib/api/me";
import { accessLossReasonText, type RepositoryAccess } from "@/lib/api/repositories";

/** What the owner can do about lost access (FR-015). */
export const RESTORE_ACCESS_GUIDANCE =
  "Restore access to the repository for the CodeAtlas GitHub App, then choose Re-index.";

/**
 * Whether CodeAtlas can read the repository on GitHub (FR-009, FR-014): when access was last
 * verified or, once it is lost, why and since when, and when the repository's data is deleted.
 */
export function RepositoryAccessPanel({ access }: { access: RepositoryAccess }) {
  if (access.state === "lost") return <LostAccess access={access} />;
  if (access.state === "verified" && access.checked_at) {
    return (
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        CodeAtlas can read this repository on GitHub. Access was last verified{" "}
        <span className="font-medium text-foreground">
          <RelativeTime value={access.checked_at} />
        </span>
        .
      </p>
    );
  }
  return (
    <p className="text-sm text-zinc-600 dark:text-zinc-400">
      Access to this repository on GitHub has not been checked yet. CodeAtlas checks it before
      each indexing run.
    </p>
  );
}

function LostAccess({ access }: { access: RepositoryAccess }) {
  const { data: me } = useMe();

  return (
    <div className="flex flex-col gap-3 rounded-md border border-rose-300 bg-rose-50 px-4 py-3 text-sm text-rose-950 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-100">
      <p className="font-medium">CodeAtlas can no longer read this repository on GitHub.</p>
      <p>{accessLossReasonText(access.reason)}</p>
      <dl className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        {access.lost_at && (
          <Field label="Access lost">
            <DateTime value={access.lost_at} />
          </Field>
        )}
        {access.purge_after && (
          <Field label="Data deleted after">
            <DateTime value={access.purge_after} />
          </Field>
        )}
        {access.checked_at && (
          <Field label="Last checked">
            <RelativeTime value={access.checked_at} />
          </Field>
        )}
      </dl>
      <p>
        Its indexed versions, files, search, and answers are hidden. If access does not return
        before the date above, the repository is disconnected and its data is deleted. If it
        returns in time, everything is available again without indexing from scratch.
      </p>
      <p className="font-medium">{RESTORE_ACCESS_GUIDANCE}</p>
      {me && (
        <a
          href={me.github_app_install_url}
          target="_blank"
          rel="noreferrer"
          className="self-start font-medium underline"
        >
          Manage the GitHub App
        </a>
      )}
    </div>
  );
}

function DateTime({ value }: { value: string }) {
  return <time dateTime={value}>{new Date(value).toLocaleString()}</time>;
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs opacity-75">{label}</dt>
      <dd className="font-medium">{children}</dd>
    </div>
  );
}
