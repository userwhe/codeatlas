"use client";

import Link from "next/link";

import type { ApiError } from "@/lib/api/client";
import { accessLossReasonText, type Repository, useRepositories } from "@/lib/api/repositories";

function detail(error: ApiError, key: string) {
  const value = error.details[key];
  return typeof value === "string" ? value : null;
}

/** The lost repository named by a 403 `repository_access_lost`, once the list has loaded. */
function useLostRepository(error: ApiError): Repository | null {
  const { data } = useRepositories();
  const repositoryId = detail(error, "repository_id");
  return data?.pages.flatMap((page) => page.items).find((item) => item.id === repositoryId) ?? null;
}

/**
 * Shown in place of a version, a file, search results, or an answer when GitHub access to their
 * repository was lost (FR-014). It links to the repository page, which explains what to do.
 */
export function AccessLostNotice({ error, subject }: { error: ApiError; subject: string }) {
  const repository = useLostRepository(error);
  const repositoryId = detail(error, "repository_id");
  const purgeAfter = detail(error, "purge_after");

  return (
    <div className="flex flex-col gap-4">
      <Link
        href="/repositories"
        className="self-start text-sm text-zinc-600 hover:underline dark:text-zinc-400"
      >
        ← All repositories
      </Link>
      <div
        role="alert"
        className="flex flex-col gap-2 rounded-md border border-rose-300 bg-rose-50 px-4 py-3 text-sm text-rose-950 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-100"
      >
        <p className="font-medium">
          {subject} is hidden because CodeAtlas can no longer read its repository on GitHub.
        </p>
        <p>{accessLossReasonText(detail(error, "reason"))}</p>
        {purgeAfter && (
          <p>
            If access does not return by{" "}
            <time dateTime={purgeAfter}>{new Date(purgeAfter).toLocaleString()}</time>, the
            repository is disconnected and its data is deleted.
          </p>
        )}
        <Link
          href={repositoryId ? `/repositories/${repositoryId}` : "/repositories"}
          className="self-start font-medium underline"
        >
          {repository
            ? `Go to ${repository.full_name} to restore access`
            : repositoryId
              ? "Go to the repository to restore access"
              : "Go to your repositories to restore access"}
        </Link>
      </div>
    </div>
  );
}
