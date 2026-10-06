"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useRef } from "react";

import { api, errorMessage, unwrap } from "@/lib/api/client";
import type { Repository } from "@/lib/api/repositories";

/**
 * A modal dialog that confirms disconnecting a repository. Mount it to open it; it calls
 * `onClose` when dismissed. On success it returns to the repository list.
 */
export function DisconnectRepositoryDialog({
  repository,
  onClose,
}: {
  repository: Repository;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const router = useRouter();
  const queryClient = useQueryClient();

  useEffect(() => {
    dialogRef.current?.showModal();
  }, []);

  const disconnect = useMutation({
    mutationFn: () =>
      unwrap(
        api.DELETE("/v1/repositories/{repository_id}", {
          params: { path: { repository_id: repository.id } },
        }),
      ),
    onSuccess: () => {
      // Everything about the repository now returns 404. Mark all cached data stale without
      // refetching it here, so this page does not flash a "not found" error while leaving.
      void queryClient.invalidateQueries({ refetchType: "none" });
      queryClient.removeQueries({ queryKey: ["repositories", "list"] });
      router.push("/repositories");
    },
  });

  const busy = disconnect.isPending || disconnect.isSuccess;

  return (
    <dialog
      ref={dialogRef}
      onClose={onClose}
      onCancel={(event) => {
        // Escape must not close the dialog while the request is in flight.
        if (busy) event.preventDefault();
      }}
      aria-labelledby="disconnect-title"
      aria-describedby="disconnect-description"
      className="m-auto w-[calc(100%-2rem)] max-w-md rounded-lg bg-white text-zinc-900 shadow-xl backdrop:bg-black/50 dark:bg-zinc-900 dark:text-zinc-100"
    >
      <div className="flex flex-col gap-4 p-6">
        <h2 id="disconnect-title" className="text-lg font-semibold break-all">
          Disconnect {repository.full_name}?
        </h2>
        <div id="disconnect-description" className="flex flex-col gap-2 text-sm">
          <p>
            CodeAtlas stops reading this repository. Its indexed versions, search data, questions,
            and answers become inaccessible right away and are permanently deleted within 24 hours.
            Queued work is canceled.
          </p>
          <p className="font-medium">This cannot be undone.</p>
          <p className="text-zinc-600 dark:text-zinc-400">
            Nothing changes on GitHub. If you connect the repository again later, it is indexed
            from scratch.
          </p>
        </div>
        {disconnect.error && (
          <p role="alert" className="text-sm text-red-700 dark:text-red-400">
            {errorMessage(disconnect.error)}
          </p>
        )}
        <div className="flex justify-end gap-3">
          <button
            type="button"
            autoFocus
            onClick={() => dialogRef.current?.close()}
            disabled={busy}
            className="rounded-md border border-zinc-300 px-4 py-1.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => disconnect.mutate()}
            disabled={busy}
            className="rounded-md bg-red-700 px-4 py-1.5 text-sm font-medium text-white hover:bg-red-600 disabled:opacity-50"
          >
            {busy ? "Disconnecting…" : "Disconnect"}
          </button>
        </div>
      </div>
    </dialog>
  );
}
