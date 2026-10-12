FROM node:24-slim AS deps

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

# Development server for Docker Compose, which mounts ./src over /app/src.
FROM deps AS dev

COPY . .

EXPOSE 3000
CMD ["npm", "run", "dev", "--", "--hostname", "0.0.0.0"]

FROM deps AS build

ENV NEXT_TELEMETRY_DISABLED=1

COPY . .

# The API's address, which Next.js fixes in its rewrites when it builds: the API's private address
# on Railway (specs/004-pilot-deployment research R3).
ARG API_ORIGIN=http://localhost:8000
ENV API_ORIGIN=$API_ORIGIN
# Inlined into the footer at build time: the platform's commit on Railway, or else
# NEXT_PUBLIC_RELEASE (research R6).
ARG NEXT_PUBLIC_RELEASE=development
ARG RAILWAY_GIT_COMMIT_SHA=
RUN NEXT_PUBLIC_RELEASE="${RAILWAY_GIT_COMMIT_SHA:-$NEXT_PUBLIC_RELEASE}" npm run build

# Release image: the standalone server, its static assets, and the release gate, without
# node_modules or sources.
FROM node:24-slim AS production

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000

WORKDIR /app

COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
# The release gate, the service's pre-deploy command (research R7).
COPY --chown=node:node scripts/wait-for-api.mjs ./scripts/

USER node

EXPOSE 3000
CMD ["node", "server.js"]
