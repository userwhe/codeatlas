"use client";

import { errorMessage } from "@/lib/api/client";
import { useCoverage } from "@/lib/api/repositories";

const REASON_LABELS: Record<string, string> = {
  excluded_directory: "Excluded directory (dependencies, build output, or version control)",
  credential_file: "Possible credentials",
  generated: "Generated or lock file",
  binary: "Binary file",
  unsupported_encoding: "Not UTF-8 text",
  too_large: "Over the 1 MiB file size limit",
  link: "Link (not followed)",
  unsafe_path: "Unsafe or unsupported entry",
  unsupported_syntax: "Indexed as text only (code structure not parsed)",
  embeddings_unavailable: "Documentation search by meaning unavailable",
};

/** Why an entry was skipped, in words; review coverage uses the same labels. */
export function reasonLabel(reason: string) {
  return REASON_LABELS[reason] ?? reason.replaceAll("_", " ");
}

/** The skipped and partially indexed entries of a ready snapshot, with their reasons. */
export function CoverageTable({ snapshotId }: { snapshotId: string }) {
  const coverage = useCoverage(snapshotId);
  const entries = coverage.data?.pages.flatMap((page) => page.items) ?? [];

  if (!coverage.data) {
    if (coverage.error) {
      return (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          Could not load coverage: {errorMessage(coverage.error)}
        </p>
      );
    }
    return <p className="text-sm text-zinc-500">Loading coverage…</p>;
  }

  if (entries.length === 0) {
    return <p className="text-sm text-zinc-600 dark:text-zinc-400">Nothing was skipped.</p>;
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="overflow-x-auto rounded-lg border border-zinc-200 dark:border-zinc-800">
        <table className="w-full text-left text-sm">
          <thead className="bg-zinc-50 text-xs text-zinc-600 dark:bg-zinc-900 dark:text-zinc-400">
            <tr>
              <th scope="col" className="px-3 py-2 font-medium">
                Path
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                Type
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                Reason
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                Detail
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
            {entries.map((entry, index) => (
              <tr key={`${entry.path}:${entry.reason}:${index}`}>
                <td className="px-3 py-2 font-mono text-xs break-all">{entry.path}</td>
                <td className="px-3 py-2">{entry.entry_type === "directory" ? "Directory" : "File"}</td>
                <td className="px-3 py-2">{reasonLabel(entry.reason)}</td>
                <td className="px-3 py-2 text-zinc-600 dark:text-zinc-400">{entry.detail ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {coverage.hasNextPage && (
        <button
          type="button"
          onClick={() => void coverage.fetchNextPage()}
          disabled={coverage.isFetchingNextPage}
          className="self-center rounded-md border border-zinc-300 px-4 py-1.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
        >
          {coverage.isFetchingNextPage ? "Loading…" : "Load more"}
        </button>
      )}
    </div>
  );
}
