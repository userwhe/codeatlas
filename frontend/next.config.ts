import type { NextConfig } from "next";

// The browser talks to a single origin: the web app forwards the API's paths to the API
// (specs/004-pilot-deployment, research R3), over the private network in hosted environments and
// to localhost:8000 in development. Rewrites are fixed when Next.js builds, so API_ORIGIN must be
// set then: the image takes it as a build argument.
const apiOrigin = process.env.API_ORIGIN ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // The production image runs the self-contained server in .next/standalone.
  output: "standalone",
  async rewrites() {
    return [
      { source: "/v1/:path*", destination: `${apiOrigin}/v1/:path*` },
      { source: "/auth/:path*", destination: `${apiOrigin}/auth/:path*` },
      { source: "/webhooks/:path*", destination: `${apiOrigin}/webhooks/:path*` },
      { source: "/status/:path*", destination: `${apiOrigin}/status/:path*` },
      { source: "/readyz", destination: `${apiOrigin}/readyz` },
      { source: "/version", destination: `${apiOrigin}/version` },
    ];
  },
  async headers() {
    // The platform's edge redirects plain HTTP to HTTPS; this keeps browsers on HTTPS.
    return [
      {
        source: "/:path*",
        headers: [{ key: "Strict-Transport-Security", value: "max-age=31536000" }],
      },
    ];
  },
};

export default nextConfig;
