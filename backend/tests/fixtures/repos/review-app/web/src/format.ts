export function formatCount(count: number): string {
  return count <= 1 ? `${count} item` : `${count} items`;
}
