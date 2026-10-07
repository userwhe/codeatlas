"use client";

import { useEffect, useState } from "react";

const MINUTE_MS = 60_000;
const RELATIVE_UNITS: [unit: Intl.RelativeTimeFormatUnit, ms: number][] = [
  ["day", 24 * 60 * MINUTE_MS],
  ["hour", 60 * MINUTE_MS],
  ["minute", MINUTE_MS],
];
const RELATIVE_TIME = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

/** How long ago `time` was, such as "just now", "5 minutes ago", or "yesterday". */
function timeAgo(time: Date, now: number) {
  const elapsed = Math.max(0, now - time.getTime());
  for (const [unit, ms] of RELATIVE_UNITS) {
    if (elapsed >= ms) return RELATIVE_TIME.format(-Math.floor(elapsed / ms), unit);
  }
  return "just now";
}

/** The current time, updated every minute so that relative times stay correct. */
function useNow() {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), MINUTE_MS);
    return () => clearInterval(timer);
  }, []);
  return now;
}

/** A past time relative to now, such as "5 minutes ago", with the exact time on hover. */
export function RelativeTime({ value }: { value: string }) {
  const now = useNow();
  const time = new Date(value);
  return (
    <time dateTime={value} title={time.toLocaleString()}>
      {timeAgo(time, now)}
    </time>
  );
}
