import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import { HeaderNav } from "@/components/HeaderNav";
import { UsageIndicator } from "@/components/UsageIndicator";
import { UserMenu } from "@/components/UserMenu";

import { Providers } from "./providers";
import "./globals.css";

export const metadata: Metadata = {
  title: "CodeAtlas",
  description:
    "Ask questions about your GitHub repositories and get answers that cite the exact code.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="flex min-h-full flex-col">
        <Providers>
          <header className="border-b border-zinc-200 dark:border-zinc-800">
            <div className="mx-auto flex h-14 w-full max-w-5xl items-center justify-between px-4">
              <div className="flex items-center gap-6">
                <Link href="/" className="text-lg font-semibold">
                  CodeAtlas
                </Link>
                <HeaderNav />
              </div>
              <div className="flex items-center gap-4">
                <UsageIndicator />
                <UserMenu />
              </div>
            </div>
          </header>
          <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col px-4 py-8">
            {children}
          </main>
        </Providers>
      </body>
    </html>
  );
}
