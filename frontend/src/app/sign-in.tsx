"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { useMe } from "@/lib/api/me";

export function SignIn({ failed, notInvited }: { failed: boolean; notInvited: boolean }) {
  const router = useRouter();
  const { data: me } = useMe();

  // A user who already has a session goes straight to their repositories.
  useEffect(() => {
    if (me) router.replace("/repositories");
  }, [me, router]);

  // GitHub accounts that are not on the pilot's access list are turned away at sign-in, so
  // offering to sign in again would only repeat the refusal.
  if (notInvited) {
    return (
      <div className="mx-auto mt-16 flex w-full max-w-md flex-col gap-6 text-center">
        <h1 className="text-3xl font-semibold tracking-tight">CodeAtlas is in a private pilot</h1>
        <p className="text-zinc-600 dark:text-zinc-400">
          Your GitHub account is not on the pilot&apos;s access list. CodeAtlas did not keep any
          data from this sign-in.
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto mt-16 flex w-full max-w-md flex-col gap-6 text-center">
      <h1 className="text-3xl font-semibold tracking-tight">Sign in to CodeAtlas</h1>
      <p className="text-zinc-600 dark:text-zinc-400">
        Connect a GitHub repository, ask where something is implemented, and get answers that
        cite the exact code.
      </p>
      {failed && (
        <p
          role="alert"
          className="rounded-md border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-800 dark:bg-red-950 dark:text-red-200"
        >
          Sign-in with GitHub did not complete. Please try again.
        </p>
      )}
      {me ? (
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          Signed in as {me.user.github_login}. Redirecting…
        </p>
      ) : (
        // A plain link: the browser must do a full navigation to the API's redirect.
        <a
          href="/auth/github/login"
          className="mx-auto rounded-md bg-zinc-900 px-5 py-2.5 font-medium text-white hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
        >
          Sign in with GitHub
        </a>
      )}
    </div>
  );
}
