import { Suspense } from "react";

import { BrowseView } from "./browse-view";

export default async function BrowsePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  // The view reads `path` and `lines` from the URL on the client.
  return (
    <Suspense fallback={<p className="text-sm text-zinc-500">Loading…</p>}>
      <BrowseView key={id} snapshotId={id} />
    </Suspense>
  );
}
