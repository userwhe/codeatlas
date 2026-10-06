# Architecture

## Overview

The backend is a plain Python package under `app/`. The frontend is a TypeScript application
under `src/` that renders users with React components.

## Access control

Every read goes through `check_repository_access` in `app/auth/access.py`. It looks up the
user's membership for the repository and asks `AccessPolicy.can_read` whether the role may read
it. Owners and members may read; guests may not. A user without a membership is denied.

## Frontend

`UserService` in `src/services/userService.ts` keeps users in memory and answers
`getUserById`. The `UserCard` component formats a user with `formatUser` from
`src/utils/format.ts`.

## Generated code

`src/generated/apiClient.ts` is produced from the service schema and is marked
`linguist-generated` in `.gitattributes`.
