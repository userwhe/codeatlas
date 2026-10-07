import type { RepositoryState } from "@/lib/api/repositories";

const STYLES: Record<RepositoryState, string> = {
  access_lost: "bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-200",
  indexing: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-200",
  ready: "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-200",
  rejected: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
  failed: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200",
};

const LABELS: Record<RepositoryState, string> = {
  access_lost: "Access lost",
  indexing: "Indexing",
  ready: "Ready",
  rejected: "Rejected",
  failed: "Failed",
};

export function RepositoryStateBadge({ state }: { state: RepositoryState }) {
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${STYLES[state]}`}>
      {LABELS[state]}
    </span>
  );
}
