"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { useMe } from "@/lib/api/me";

/** Navigation for signed-in users. Renders nothing while loading or when anonymous. */
export function HeaderNav() {
  const { data: me } = useMe();
  const pathname = usePathname();
  if (!me) return null;

  // Answers and indexed versions belong to a repository.
  const current = ["/repositories", "/answers", "/snapshots"].some((prefix) =>
    pathname.startsWith(prefix),
  );
  return (
    <nav aria-label="Main" className="text-sm">
      <Link
        href="/repositories"
        aria-current={current ? "page" : undefined}
        className={
          current
            ? "font-medium"
            : "text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
        }
      >
        Repositories
      </Link>
    </nav>
  );
}
