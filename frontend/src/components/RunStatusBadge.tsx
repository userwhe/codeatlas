import { type QualityState, type RunStatus, runStateLabel } from "@/lib/api/questions";

const ACTIVE = "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-200";
const NEUTRAL = "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300";

function style(status: RunStatus, quality: QualityState) {
  switch (status) {
    case "succeeded":
      return quality === "insufficient_evidence"
        ? "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200"
        : "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-200";
    case "failed":
      return "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200";
    case "canceled":
      return NEUTRAL;
    default:
      return ACTIVE;
  }
}

export function RunStatusBadge({ status, quality }: { status: RunStatus; quality: QualityState }) {
  return (
    <span
      className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${style(status, quality)}`}
    >
      {runStateLabel(status, quality)}
    </span>
  );
}
