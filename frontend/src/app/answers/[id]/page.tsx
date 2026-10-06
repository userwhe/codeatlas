import { AnswerDetail } from "./answer-detail";

export default async function AnswerPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  // The key resets page state (such as expanded citations) when moving between answers.
  return <AnswerDetail key={id} runId={id} />;
}
