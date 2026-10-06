"use client";

import Image from "next/image";
import { useState } from "react";

import { useMe } from "@/lib/api/me";

/** The signed-in user and a sign-out button. Renders nothing while loading or when anonymous. */
export function UserMenu() {
  const { data: me } = useMe();
  const [signingOut, setSigningOut] = useState(false);
  const [failed, setFailed] = useState(false);

  if (!me) return null;

  async function signOut() {
    setSigningOut(true);
    setFailed(false);
    try {
      const response = await fetch("/auth/logout", {
        method: "POST",
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error(`Sign-out failed with status ${response.status}`);
      // A full navigation drops all cached data from the signed-in session.
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination -- intentional
      window.location.assign("/");
    } catch {
      setFailed(true);
      setSigningOut(false);
    }
  }

  return (
    <div className="flex items-center gap-3 text-sm">
      {me.user.avatar_url && (
        <Image
          src={me.user.avatar_url}
          alt=""
          width={28}
          height={28}
          unoptimized
          className="rounded-full"
        />
      )}
      <span className="font-medium">{me.user.github_login}</span>
      <button
        type="button"
        onClick={signOut}
        disabled={signingOut}
        className="rounded-md border border-zinc-300 px-3 py-1 hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
      >
        {signingOut ? "Signing out…" : "Sign out"}
      </button>
      {failed && (
        <span role="alert" className="text-red-600 dark:text-red-400">
          Sign-out failed. Try again.
        </span>
      )}
    </div>
  );
}
