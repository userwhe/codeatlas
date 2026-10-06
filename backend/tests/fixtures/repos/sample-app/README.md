# Sample App

A small application with a Python backend and a TypeScript frontend. It manages which users
may read which repositories.

## Setup

1. Install Python 3.13 and Node.js 24.
2. Copy `.env.example` to `.env` and fill in `API_TOKEN`.
3. Run `npm install` for the frontend packages.
4. Start the backend with `python -m app.main`.

## Usage

`describe_access` in `app/main.py` prints whether a user can read a repository. The frontend
shows each user with the `UserCard` component.

## Project layout

- `app/`: Python backend. Access rules live in `app/auth/access.py`.
- `src/`: TypeScript frontend.
- `docs/`: design notes, starting with `docs/architecture.md`.
