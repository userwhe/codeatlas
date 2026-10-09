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

# Inlined into the footer at build time; the release job passes the commit SHA.
ARG NEXT_PUBLIC_RELEASE=development
RUN npm run build

# Release image: the standalone server and its static assets, without node_modules or sources.
FROM node:24-slim AS production

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000

WORKDIR /app

COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static

USER node

EXPOSE 3000
CMD ["node", "server.js"]
