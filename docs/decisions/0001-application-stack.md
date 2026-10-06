# ADR 0001: Application stack

- **Status**: Accepted
- **Date**: 2026-10-03
- **Scope**: All features, starting with `specs/001-repository-qa`

## Context

CodeAtlas needs four things: a browser UI; an HTTP API that owns authorization and job creation;
a background worker that fetches and parses repositories and calls model providers; and a typed
contract between the UI and the API. The worker depends on source parsing (tree-sitter) and on
model-provider SDKs (ADR 0004 and ADR 0006).

## Options considered

1. TypeScript and Next.js frontend; Python and FastAPI API; a Python worker sharing the API's
   package.
2. Java and Spring Boot API with a Python worker.
3. TypeScript end to end: a Next.js frontend, plus a NestJS API and worker.

## Decision

Option 1:

- **Backend**: Python 3.13, FastAPI, Pydantic 2, SQLAlchemy 2.1 (synchronous), Alembic.
- **Frontend**: TypeScript 5.9, Next.js 16 (App Router), React 19, Tailwind CSS 4, TanStack
  Query.
- **Contract**: frontend types are generated from the API's OpenAPI document with
  openapi-typescript.
- **Tooling**: uv, Ruff, mypy, pytest, ESLint, Playwright.

## Trade-off

- **Gained**:
  - The best-supported ecosystem for parsing and model SDKs.
  - An API contract generated from one source.
  - One backend language shared by the API and the worker.
- **Accepted**:
  - Two languages in the repository, Python and TypeScript.
  - A build step that regenerates frontend types whenever the API changes.
  - TypeScript stays on 5.9 until Next.js supports the 7.x compiler.

Details: `specs/001-repository-qa/research.md` R1.
