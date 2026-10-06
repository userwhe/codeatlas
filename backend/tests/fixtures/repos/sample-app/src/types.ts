export enum Role {
  Owner = "owner",
  Member = "member",
  Guest = "guest",
}

export interface User {
  id: number;
  login: string;
  role: Role;
}

export type UserId = User["id"];
