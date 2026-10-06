import type { NextConfig } from "next";

// The browser talks to a single origin: /v1/* and /auth/* are proxied to the API (research R6).
const apiOrigin = process.env.API_ORIGIN ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      { source: "/v1/:path*", destination: `${apiOrigin}/v1/:path*` },
      { source: "/auth/:path*", destination: `${apiOrigin}/auth/:path*` },
    ];
  },
};

export default nextConfig;
