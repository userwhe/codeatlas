"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { ApiError } from "@/lib/api/client";

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Client errors (401, 404, ...) do not get better on retry; server errors may.
        retry: (failureCount, error) =>
          failureCount < 3 && !(error instanceof ApiError && error.status < 500),
      },
    },
  });
}

let browserQueryClient: QueryClient | undefined;

function getQueryClient() {
  // Keep server renders isolated and reuse one client in the browser.
  if (typeof window === "undefined") return makeQueryClient();
  browserQueryClient ??= makeQueryClient();
  return browserQueryClient;
}

export function Providers({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={getQueryClient()}>{children}</QueryClientProvider>;
}
