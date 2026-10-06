import { Role, User, UserId } from "../types";

export class UserService {
  private readonly users = new Map<UserId, User>();

  add(user: User): void {
    this.users.set(user.id, user);
  }

  getUserById(id: UserId): User | undefined {
    return this.users.get(id);
  }

  isOwner(id: UserId): boolean {
    return this.users.get(id)?.role === Role.Owner;
  }
}
