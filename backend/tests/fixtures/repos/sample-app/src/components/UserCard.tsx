import { User } from "../types";
import { formatUser, initials } from "../utils/format";

export interface UserCardProps {
  user: User;
}

export function UserCard({ user }: UserCardProps) {
  return (
    <div className="user-card">
      <span className="avatar">{initials(user.login)}</span>
      <span className="name">{formatUser(user)}</span>
    </div>
  );
}
