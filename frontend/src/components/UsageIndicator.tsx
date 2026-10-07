"use client";

import { useMe } from "@/lib/api/me";
import { useUsage } from "@/lib/api/questions";

const NORMAL = "text-zinc-600 dark:text-zinc-400";
const EXHAUSTED = "font-medium text-amber-700 dark:text-amber-400";

/**
 * Today's question and review usage of the workspace. Renders nothing while loading or when
 * anonymous.
 */
export function UsageIndicator() {
  const { data: me } = useMe();
  const { data: usage } = useUsage({ enabled: Boolean(me) });
  if (!me || !usage) return null;

  const resetsAt = new Date(usage.resets_at).toLocaleString();
  return (
    <p title={`The allowances reset at ${resetsAt}.`} className="flex gap-3 text-sm">
      <span className={usage.questions_used >= usage.questions_limit ? EXHAUSTED : NORMAL}>
        Questions today:{" "}
        <span className="tabular-nums">
          {usage.questions_used} / {usage.questions_limit}
        </span>
      </span>
      <span className={usage.reviews_used >= usage.reviews_limit ? EXHAUSTED : NORMAL}>
        Reviews today:{" "}
        <span className="tabular-nums">
          {usage.reviews_used} / {usage.reviews_limit}
        </span>
      </span>
    </p>
  );
}
