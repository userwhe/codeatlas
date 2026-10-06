"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { ExternalProcessingAcceptance } from "@/components/ExternalProcessingAcceptance";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { useMe } from "@/lib/api/me";
import { type GitHubRepository, useGitHubRepositories } from "@/lib/api/repositories";

interface ConnectRequest {
  github_repository_id: number;
  branch?: string;
  accept_external_processing: boolean;
}

/**
 * A modal dialog that connects one of the user's GitHub repositories. Mount it to open it; it
 * calls `onClose` when dismissed. On success it navigates to the new repository's page.
 */
export function ConnectRepositoryDialog({ onClose }: { onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const router = useRouter();
  const queryClient = useQueryClient();
  const { data: me } = useMe();
  const candidates = useGitHubRepositories();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [branch, setBranch] = useState("");
  const [accepted, setAccepted] = useState(false);

  useEffect(() => {
    dialogRef.current?.showModal();
  }, []);

  const connect = useMutation({
    mutationFn: ({ body, idempotencyKey }: { body: ConnectRequest; idempotencyKey: string }) =>
      unwrap(
        api.POST("/v1/repositories", {
          body,
          params: { header: { "Idempotency-Key": idempotencyKey } },
        }),
      ),
    onSuccess: ({ repository }) => {
      void queryClient.invalidateQueries({ queryKey: ["repositories"] });
      void queryClient.invalidateQueries({ queryKey: ["github-repositories"] });
      router.push(`/repositories/${repository.id}`);
    },
  });

  const repositories = candidates.data?.pages.flatMap((page) => page.items) ?? [];
  const selected = repositories.find((item) => item.github_repository_id === selectedId);
  const busy = connect.isPending || connect.isSuccess;

  function select(repository: GitHubRepository) {
    setSelectedId(repository.github_repository_id);
    setAccepted(false);
    connect.reset();
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected || busy) return;
    const trimmedBranch = branch.trim();
    connect.mutate({
      body: {
        github_repository_id: selected.github_repository_id,
        ...(trimmedBranch ? { branch: trimmedBranch } : {}),
        accept_external_processing: selected.private && accepted,
      },
      // A new key for each submission attempt; a replay of the same request cannot connect twice.
      idempotencyKey: crypto.randomUUID(),
    });
  }

  return (
    <dialog
      ref={dialogRef}
      onClose={onClose}
      aria-labelledby="connect-repository-title"
      className="m-auto w-[calc(100%-2rem)] max-w-lg rounded-lg bg-white text-zinc-900 shadow-xl backdrop:bg-black/50 dark:bg-zinc-900 dark:text-zinc-100"
    >
      <form onSubmit={submit} className="flex flex-col gap-5 p-6">
        <div className="flex flex-col gap-1">
          <h2 id="connect-repository-title" className="text-lg font-semibold">
            Connect a repository
          </h2>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Choose a repository that the CodeAtlas GitHub App can read.
            {me && (
              <>
                {" "}
                <a
                  href={me.github_app_install_url}
                  target="_blank"
                  rel="noreferrer"
                  className="font-medium underline"
                >
                  Install the GitHub App
                </a>{" "}
                to grant access to more repositories.
              </>
            )}
          </p>
        </div>

        <fieldset className="flex flex-col gap-2">
          <legend className="mb-2 text-sm font-medium">Repository</legend>
          {candidates.data ? (
            repositories.length > 0 ? (
              <ul className="max-h-72 divide-y divide-zinc-200 overflow-y-auto rounded-md border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
                {repositories.map((repository) => (
                  <li key={repository.github_repository_id}>
                    <CandidateRow
                      repository={repository}
                      checked={repository.github_repository_id === selectedId}
                      onSelect={() => select(repository)}
                    />
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-zinc-600 dark:text-zinc-400">
                No repositories are available yet. Install the GitHub App on the repositories you
                want CodeAtlas to read.
              </p>
            )
          ) : candidates.error ? (
            <p role="alert" className="text-sm text-red-700 dark:text-red-400">
              Could not load your GitHub repositories: {errorMessage(candidates.error)}{" "}
              <button
                type="button"
                onClick={() => void candidates.refetch()}
                className="font-medium underline"
              >
                Try again
              </button>
            </p>
          ) : (
            <p className="text-sm text-zinc-500">Loading your GitHub repositories…</p>
          )}
          {candidates.hasNextPage && (
            <button
              type="button"
              onClick={() => void candidates.fetchNextPage()}
              disabled={candidates.isFetchingNextPage}
              className="self-start text-sm font-medium underline disabled:opacity-50"
            >
              {candidates.isFetchingNextPage ? "Loading…" : "Load more"}
            </button>
          )}
        </fieldset>

        <div className="flex flex-col gap-1">
          <label htmlFor="connect-branch" className="text-sm font-medium">
            Branch (optional)
          </label>
          <input
            id="connect-branch"
            value={branch}
            onChange={(event) => setBranch(event.target.value)}
            placeholder={selected?.default_branch ?? "default branch"}
            maxLength={255}
            className="rounded-md border border-zinc-300 bg-transparent px-3 py-1.5 text-sm dark:border-zinc-700"
          />
          <p className="text-xs text-zinc-500">Leave empty to index the default branch.</p>
        </div>

        {selected?.private && (
          <ExternalProcessingAcceptance checked={accepted} onChange={setAccepted} />
        )}

        {connect.error && (
          <p role="alert" className="text-sm text-red-700 dark:text-red-400">
            {errorMessage(connect.error)}
          </p>
        )}

        <div className="flex justify-end gap-3">
          <button
            type="button"
            onClick={() => dialogRef.current?.close()}
            className="rounded-md border border-zinc-300 px-4 py-1.5 text-sm hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-800"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={!selected || busy}
            className="rounded-md bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
          >
            {busy ? "Connecting…" : "Connect"}
          </button>
        </div>
      </form>
    </dialog>
  );
}

function CandidateRow({
  repository,
  checked,
  onSelect,
}: {
  repository: GitHubRepository;
  checked: boolean;
  onSelect: () => void;
}) {
  const description = (
    <span className="flex min-w-0 flex-1 flex-col">
      <span className="truncate font-medium">{repository.full_name}</span>
      <span className="text-xs text-zinc-500">
        {repository.private ? "Private" : "Public"} · default branch {repository.default_branch}
      </span>
    </span>
  );

  if (repository.connected_repository_id) {
    return (
      <div className="flex items-center gap-3 px-3 py-2 text-sm">
        {description}
        <span className="text-xs text-zinc-500">Connected</span>
        <Link
          href={`/repositories/${repository.connected_repository_id}`}
          className="text-xs font-medium underline"
        >
          Open
        </Link>
      </div>
    );
  }

  return (
    <label className="flex cursor-pointer items-center gap-3 px-3 py-2 text-sm hover:bg-zinc-50 dark:hover:bg-zinc-800/50">
      <input
        type="radio"
        name="github-repository"
        checked={checked}
        onChange={onSelect}
        required
      />
      {description}
    </label>
  );
}
