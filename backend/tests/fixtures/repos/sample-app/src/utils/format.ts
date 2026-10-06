import { User } from "../types";

export const formatUser = (user: User): string => `${user.login} (#${user.id})`;

export function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter((part) => part.length > 0)
    .map((part) => part[0].toUpperCase())
    .join("");
}
