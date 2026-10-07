"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import { AccessLostNotice } from "@/components/AccessLostNotice";
import { ApiError, errorMessage, isAccessLost } from "@/lib/api/client";
import { shortSha, useRepository, useSnapshot } from "@/lib/api/repositories";

type View = "browse" | "search";

const VIEWS: [view: View, label: string][] = [
  ["browse", "Browse files"],
  ["search", "Search"],
];

function RepositoryName({ repositoryId }: { repositoryId: string }) {
  const { data: repository } = useRepository(repositoryId);
  return <>{repository?.full_name ?? "Repository"}</>;
}

/**
 * The header of a snapshot page: the repository, the commit, and links between browsing and
 * searching. Renders `children` only once the snapshot is known to be readable. If GitHub access
 * to the repository is lost, even after the snapshot loaded, it shows a notice instead.
 */
export function SnapshotFrame({
  snapshotId,
  current,
  children,
}: {
  snapshotId: string;
  current: View;
  children: ReactNode;
}) {
  const { data: snapshot, error } = useSnapshot(snapshotId);

  if (isAccessLost(error)) return <AccessLostNotice error={error} subject="This version" />;

  if (!snapshot) {
    if (error) {
      const missing = error instanceof ApiError && (error.status === 404 || error.status === 422);
      const notReady = error instanceof ApiError && error.code === "snapshot_not_ready";
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
              ? "This indexed version does not exist or is not available in your workspace."
              : notReady
                ? "This version is not ready yet. Try again when indexing has finished."
                : `Could not load this version: ${errorMessage(error)}`}
          </p>
        </div>
      );
    }
    return <p className="text-sm text-zinc-500">Loading…</p>;
  }

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-3">
        <Link
          href={`/repositories/${snapshot.repository_id}`}
          className="self-start text-sm text-zinc-600 hover:underline dark:text-zinc-400"
        >
          ← <RepositoryName repositoryId={snapshot.repository_id} />
        </Link>
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">
            {current === "browse" ? "Files" : "Search"}
          </h1>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            at commit{" "}
            <code title={snapshot.commit_sha} className="font-medium">
              {shortSha(snapshot.commit_sha)}
            </code>{" "}
            on {snapshot.branch}
          </p>
        </div>
        <nav
          aria-label="Version views"
          className="flex gap-1 border-b border-zinc-200 text-sm dark:border-zinc-800"
        >
          {VIEWS.map(([view, label]) => (
            <Link
              key={view}
              href={`/snapshots/${snapshotId}/${view}`}
              aria-current={view === current ? "page" : undefined}
              className={`-mb-px border-b-2 px-3 py-2 ${
                view === current
                  ? "border-zinc-900 font-medium dark:border-zinc-100"
                  : "border-transparent text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
              }`}
            >
              {label}
            </Link>
          ))}
        </nav>
      </header>
      {children}
    </div>
  );
}
