import { Suspense } from "react";

import { SearchView } from "./search-view";

export default async function SearchPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  // The view reads `mode` and `q` from the URL on the client.
  return (
    <Suspense fallback={<p className="text-sm text-zinc-500">Loading…</p>}>
      <SearchView key={id} snapshotId={id} />
    </Suspense>
  );
}
