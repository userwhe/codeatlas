"use client";

import { useMe } from "@/lib/api/me";
import { useUsage } from "@/lib/api/questions";

/** Today's question usage of the workspace. Renders nothing while loading or when anonymous. */
export function UsageIndicator() {
  const { data: me } = useMe();
  const { data: usage } = useUsage({ enabled: Boolean(me) });
  if (!me || !usage) return null;

  const exhausted = usage.questions_used >= usage.questions_limit;
  const resetsAt = new Date(usage.resets_at).toLocaleString();
  return (
    <p
      title={`The allowance resets at ${resetsAt}.`}
      className={
        exhausted
          ? "text-sm font-medium text-amber-700 dark:text-amber-400"
          : "text-sm text-zinc-600 dark:text-zinc-400"
      }
    >
      Questions today:{" "}
      <span className="tabular-nums">
        {usage.questions_used} / {usage.questions_limit}
      </span>
    </p>
  );
}
