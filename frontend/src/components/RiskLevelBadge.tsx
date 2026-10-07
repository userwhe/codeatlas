import type { RiskLevel } from "@/lib/api/reviews";

const STYLES: Record<RiskLevel, string> = {
  high: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200",
  medium: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
  low: "bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-200",
  none: "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-200",
};

const LABELS: Record<RiskLevel, string> = {
  high: "High",
  medium: "Medium",
  low: "Low",
  none: "None",
};

/**
 * A risk level: the overall level of a review, or the severity of one risk. `label` is read
 * before the level by screen readers, such as "Severity".
 */
export function RiskLevelBadge({ level, label }: { level: RiskLevel; label?: string }) {
  return (
    <span
      className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${STYLES[level]}`}
    >
      {label && <span className="sr-only">{label}: </span>}
      {LABELS[level]}
    </span>
  );
}
