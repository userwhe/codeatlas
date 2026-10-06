"use client";

import { useSearchParams } from "next/navigation";

import { CodeView } from "@/components/CodeView";
import { FileTree } from "@/components/FileTree";
import { SnapshotFrame } from "@/components/SnapshotFrame";
import { parseLineRange } from "@/lib/api/browse";

/** The file tree and the selected file (`?path=`), with `?lines=<start>-<end>` highlighted. */
export function BrowseView({ snapshotId }: { snapshotId: string }) {
  const searchParams = useSearchParams();
  const path = searchParams.get("path") || null;
  const range = parseLineRange(searchParams.get("lines"));

  return (
    <SnapshotFrame snapshotId={snapshotId} current="browse">
      <div className="grid grid-cols-1 gap-6 md:grid-cols-[15rem_minmax(0,1fr)]">
        <div className="max-h-80 overflow-y-auto rounded-lg border border-zinc-200 py-2 md:sticky md:top-4 md:max-h-[calc(100vh-2rem)] md:self-start dark:border-zinc-800">
          <FileTree snapshotId={snapshotId} selectedPath={path} />
        </div>
        <div className="min-w-0">
          {path ? (
            <CodeView key={path} snapshotId={snapshotId} path={path} range={range} />
          ) : (
            <p className="text-sm text-zinc-600 dark:text-zinc-400">
              Select a file to view it.
            </p>
          )}
        </div>
      </div>
    </SnapshotFrame>
  );
}
