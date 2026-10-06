import { RepositoryDetail } from "./repository-detail";

export default async function RepositoryPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  // The key resets page state (such as the followed job) when moving between repositories.
  return <RepositoryDetail key={id} repositoryId={id} />;
}
