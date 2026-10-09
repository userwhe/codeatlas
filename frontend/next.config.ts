import type { NextConfig } from "next";

// The browser talks to a single origin: /v1/* and /auth/* are proxied to the API (research R6).
// In development Next.js proxies them; in the pilot, Caddy routes them to the API before Next.js.
const apiOrigin = process.env.API_ORIGIN ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // The production image runs the self-contained server in .next/standalone.
  output: "standalone",
  async rewrites() {
    return [
      { source: "/v1/:path*", destination: `${apiOrigin}/v1/:path*` },
      { source: "/auth/:path*", destination: `${apiOrigin}/auth/:path*` },
    ];
  },
};

export default nextConfig;
