"use client";

import Link from "next/link";
import { useState } from "react";

import { errorMessage } from "@/lib/api/client";
import { type SnapshotListItem, shortSha, useSnapshots } from "@/lib/api/repositories";

function optionLabel(snapshot: SnapshotListItem) {
  const readyAt = snapshot.ready_at ? new Date(snapshot.ready_at).toLocaleString() : "unknown time";
  const active = snapshot.is_active ? " (active)" : "";
  return `${shortSha(snapshot.commit_sha)} on ${snapshot.branch}, indexed ${readyAt}${active}`;
}

const LINK_CLASS =
  "rounded-md border border-zinc-300 px-3 py-1.5 text-sm font-medium hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-800";

/** Every ready indexed version of a repository, with links to browse or search the selected one. */
export function SnapshotSelector({ repositoryId }: { repositoryId: string }) {
  const snapshots = useSnapshots(repositoryId);
  // Until the user picks one, follow the active version (it changes after a re-index).
  const [chosenId, setChosenId] = useState<string | null>(null);
  const items = snapshots.data?.pages.flatMap((page) => page.items) ?? [];

  if (!snapshots.data) {
    if (snapshots.error) {
      return (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          Could not load the indexed versions: {errorMessage(snapshots.error)}
        </p>
      );
    }
    return <p className="text-sm text-zinc-500">Loading indexed versions…</p>;
  }
  if (items.length === 0) return null;

  const selected =
    items.find((item) => item.id === chosenId) ??
    items.find((item) => item.is_active) ??
    items[0];

  return (
    <div className="flex flex-col gap-2">
      <label htmlFor="snapshot-select" className="text-sm font-medium">
        Browse or search a version
      </label>
      <div className="flex flex-wrap items-center gap-3">
        <select
          id="snapshot-select"
          value={selected.id}
          onChange={(event) => setChosenId(event.target.value)}
          className="max-w-full min-w-0 rounded-md border border-zinc-300 bg-transparent px-3 py-1.5 text-sm dark:border-zinc-700"
        >
          {items.map((snapshot) => (
            <option key={snapshot.id} value={snapshot.id}>
              {optionLabel(snapshot)}
            </option>
          ))}
        </select>
        <Link href={`/snapshots/${selected.id}/browse`} className={LINK_CLASS}>
          Browse files
        </Link>
        <Link href={`/snapshots/${selected.id}/search`} className={LINK_CLASS}>
          Search
        </Link>
        {snapshots.hasNextPage && (
          <button
            type="button"
            onClick={() => void snapshots.fetchNextPage()}
            disabled={snapshots.isFetchingNextPage}
            className="text-sm font-medium underline disabled:opacity-50"
          >
            {snapshots.isFetchingNextPage ? "Loading…" : "Load older versions"}
          </button>
        )}
      </div>
    </div>
  );
}
