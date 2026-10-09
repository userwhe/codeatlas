import { ReviewDetail } from "./review-detail";

export default async function ReviewPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  // The key resets page state (such as expanded citations) when moving between reviews.
  return <ReviewDetail key={id} runId={id} />;
}
