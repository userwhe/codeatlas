"use client";

import Link from "next/link";
import { useState } from "react";

import { ConnectRepositoryDialog } from "@/components/ConnectRepositoryDialog";
import { RepositoryStateBadge } from "@/components/RepositoryStateBadge";
import { errorMessage } from "@/lib/api/client";
import { type Repository, shortSha, useRepositories } from "@/lib/api/repositories";

export default function RepositoriesPage() {
  const repositories = useRepositories();
  const [connecting, setConnecting] = useState(false);
  const items = repositories.data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">Repositories</h1>
        <button
          type="button"
          onClick={() => setConnecting(true)}
          className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
        >
          Connect repository
        </button>
      </div>

      {repositories.data ? (
        items.length > 0 ? (
          <ul className="divide-y divide-zinc-200 rounded-lg border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
            {items.map((repository) => (
              <li key={repository.id}>
                <RepositoryRow repository={repository} />
              </li>
            ))}
          </ul>
        ) : (
          <p className="rounded-lg border border-dashed border-zinc-300 px-4 py-8 text-center text-sm text-zinc-600 dark:border-zinc-700 dark:text-zinc-400">
            No repositories are connected yet. Connect one to start indexing.
          </p>
        )
      ) : repositories.error ? (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          Could not load repositories: {errorMessage(repositories.error)}
        </p>
      ) : (
        <p className="text-sm text-zinc-500">Loading repositories…</p>
      )}

      {repositories.hasNextPage && (
        <button
          type="button"
          onClick={() => void repositories.fetchNextPage()}
          disabled={repositories.isFetchingNextPage}
          className="self-center rounded-md border border-zinc-300 px-4 py-1.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
        >
          {repositories.isFetchingNextPage ? "Loading…" : "Load more"}
        </button>
      )}

      {connecting && <ConnectRepositoryDialog onClose={() => setConnecting(false)} />}
    </div>
  );
}

function RepositoryRow({ repository }: { repository: Repository }) {
  const snapshot = repository.active_snapshot;
  return (
    <Link
      href={`/repositories/${repository.id}`}
      className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3 hover:bg-zinc-50 dark:hover:bg-zinc-900"
    >
      <span className="min-w-0 flex-1 truncate font-medium">{repository.full_name}</span>
      <span className="text-xs text-zinc-500">{repository.private ? "Private" : "Public"}</span>
      <span className="text-xs text-zinc-500">
        {snapshot ? (
          <>
            <code title={snapshot.commit_sha}>{shortSha(snapshot.commit_sha)}</code> on{" "}
            {snapshot.branch}
          </>
        ) : (
          "Not indexed yet"
        )}
      </span>
      <RepositoryStateBadge state={repository.state} />
    </Link>
  );
}
