# CodeAtlas web app

The Next.js (App Router) frontend for CodeAtlas. It talks to the FastAPI backend in `../backend`
through same-origin rewrites: `/v1/*` and `/auth/*` are proxied to `API_ORIGIN`
(default `http://localhost:8000`), so the session cookie never crosses origins.

## Requirements

- Node.js 24 (see `.nvmrc`)
- The API running on `API_ORIGIN` for anything beyond a static build

## Commands

```bash
npm ci                # install dependencies
npm run dev           # dev server on http://localhost:3000
npm run lint          # ESLint
npm run typecheck     # TypeScript, no emit
npm run build         # production build
npm run gen:api       # regenerate src/lib/api/schema.d.ts from http://localhost:8000/openapi.json
npm run test:e2e      # Playwright end-to-end tests
```

`API_ORIGIN` is read when the dev server starts or when `npm run build` runs, so set it before
either command when the API is not on `http://localhost:8000`.

Run `npm run gen:api` with the API running whenever backend routes or models change, and commit
the regenerated `src/lib/api/schema.d.ts`.

## Layout

- `src/app/`: routes, the root layout, and the TanStack Query provider
- `src/components/`: shared components such as `JobProgress`
- `src/lib/api/`: the typed API client (`client.ts`) and the generated schema (`schema.d.ts`)
