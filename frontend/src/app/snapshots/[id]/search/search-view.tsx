"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { type FormEvent, useState } from "react";

import { SnapshotFrame } from "@/components/SnapshotFrame";
import {
  browseUrl,
  MAX_QUERY_CHARS,
  SEARCH_MODES,
  type SearchMode,
  type SearchResult,
  useSearch,
} from "@/lib/api/browse";
import { errorMessage } from "@/lib/api/client";

const MODE_LABELS: Record<SearchMode, string> = {
  text: "Text",
  path: "Path",
  symbol: "Symbol",
  docs: "Docs",
};

const MODE_HELP: Record<SearchMode, string> = {
  text: "Finds text in file contents, ignoring case.",
  path: "Finds files by path. Exact paths come first.",
  symbol: "Finds functions, classes, and other declarations by name. Exact names come first.",
  docs: "Finds documentation passages that match a plain-language query.",
};

function isMode(value: string | null): value is SearchMode {
  return SEARCH_MODES.includes(value as SearchMode);
}

function validationError(query: string) {
  if (query.length === 0) return "Enter a search query.";
  if (query.length > MAX_QUERY_CHARS) {
    return `A search query can be at most ${MAX_QUERY_CHARS} characters.`;
  }
  return null;
}

/** Updates the URL in place; Next.js keeps `useSearchParams` in sync with the History API. */
function setSearchUrl(mode: SearchMode, query: string, push: boolean) {
  const params = new URLSearchParams({ mode });
  if (query) params.set("q", query);
  const url = `?${params.toString()}`;
  if (push) window.history.pushState(null, "", url);
  else window.history.replaceState(null, "", url);
}

/** Search within one indexed version; the mode (`?mode=`) and query (`?q=`) live in the URL. */
export function SearchView({ snapshotId }: { snapshotId: string }) {
  const searchParams = useSearchParams();
  const modeParam = searchParams.get("mode");
  const mode: SearchMode = isMode(modeParam) ? modeParam : "text";
  const query = (searchParams.get("q") ?? "").trim();

  return (
    <SnapshotFrame snapshotId={snapshotId} current="search">
      <div className="flex flex-col gap-6">
        {/* The key resets the input when the URL's query changes, e.g. on Back. */}
        <SearchForm key={query} mode={mode} query={query} />
        {query && validationError(query) === null && (
          <SearchResults snapshotId={snapshotId} mode={mode} query={query} />
        )}
      </div>
    </SnapshotFrame>
  );
}

function SearchForm({ mode, query }: { mode: SearchMode; query: string }) {
  const [input, setInput] = useState(query);
  const [error, setError] = useState<string | null>(() =>
    query ? validationError(query) : null,
  );

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = input.trim();
    const problem = validationError(trimmed);
    setError(problem);
    if (problem === null) setSearchUrl(mode, trimmed, true);
  }

  // Switching modes re-runs the last submitted query in the new mode.
  function changeMode(next: SearchMode) {
    setSearchUrl(next, query, false);
  }

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-3">
      <fieldset className="flex flex-col gap-2">
        <legend className="sr-only">Search mode</legend>
        <div className="flex flex-wrap gap-1 self-start rounded-lg border border-zinc-200 p-1 dark:border-zinc-800">
          {SEARCH_MODES.map((value) => (
            <label
              key={value}
              className="cursor-pointer rounded-md px-3 py-1 text-sm has-checked:bg-zinc-900 has-checked:font-medium has-checked:text-white has-focus-visible:ring-2 has-focus-visible:ring-blue-500 dark:has-checked:bg-zinc-100 dark:has-checked:text-zinc-900"
            >
              <input
                type="radio"
                name="mode"
                value={value}
                checked={value === mode}
                onChange={() => changeMode(value)}
                className="sr-only"
              />
              {MODE_LABELS[value]}
            </label>
          ))}
        </div>
        <p className="text-xs text-zinc-500">{MODE_HELP[mode]}</p>
      </fieldset>
      <div className="flex flex-wrap items-start gap-3">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <label htmlFor="search-query" className="sr-only">
            Search query
          </label>
          <input
            id="search-query"
            type="search"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            maxLength={MAX_QUERY_CHARS}
            placeholder={mode === "docs" ? "How do I set up the project?" : "Search this version"}
            aria-invalid={error !== null}
            aria-describedby={error ? "search-query-error" : undefined}
            className="rounded-md border border-zinc-300 bg-transparent px-3 py-1.5 text-sm dark:border-zinc-700"
          />
          {error && (
            <p
              id="search-query-error"
              role="alert"
              className="text-sm text-red-700 dark:text-red-400"
            >
              {error}
            </p>
          )}
        </div>
        <button
          type="submit"
          className="rounded-md bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
        >
          Search
        </button>
      </div>
    </form>
  );
}

function SearchResults({
  snapshotId,
  mode,
  query,
}: {
  snapshotId: string;
  mode: SearchMode;
  query: string;
}) {
  const search = useSearch(snapshotId, mode, query);

  if (!search.data) {
    if (search.error) {
      return (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          Search failed: {errorMessage(search.error)}
        </p>
      );
    }
    return <p className="text-sm text-zinc-500">Searching…</p>;
  }

  const { results, degraded } = search.data;
  return (
    <section aria-label="Search results" className="flex flex-col gap-3">
      {degraded && (
        <p
          role="status"
          className="rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100"
        >
          Documentation search is running in keyword-only mode.
        </p>
      )}
      <p aria-live="polite" className="text-sm text-zinc-600 dark:text-zinc-400">
        {results.length === 0
          ? `No results for “${query}”.`
          : `${results.length} ${results.length === 1 ? "result" : "results"} for “${query}”.`}
      </p>
      {results.length > 0 && (
        <ul className="flex flex-col gap-3">
          {results.map((result, index) => (
            <li key={`${result.path}:${result.start_line}:${index}`}>
              <ResultCard snapshotId={snapshotId} result={result} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ResultCard({ snapshotId, result }: { snapshotId: string; result: SearchResult }) {
  const lines =
    result.start_line === result.end_line
      ? `line ${result.start_line}`
      : `lines ${result.start_line}–${result.end_line}`;
  return (
    <Link
      href={browseUrl(snapshotId, result.path, { start: result.start_line, end: result.end_line })}
      className="flex flex-col gap-2 rounded-lg border border-zinc-200 px-4 py-3 hover:bg-zinc-50 dark:border-zinc-800 dark:hover:bg-zinc-900"
    >
      <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-mono text-sm font-medium break-all">{result.path}</span>
        <span className="text-xs text-zinc-500">{lines}</span>
        {result.symbol && (
          <span className="text-xs">
            <code className="font-medium">{result.symbol.name}</code>{" "}
            <span className="text-zinc-500">{result.symbol.kind}</span>
          </span>
        )}
        {result.exact && (
          <span className="rounded-full bg-green-100 px-2 py-0.5 text-xs font-medium text-green-800 dark:bg-green-950 dark:text-green-200">
            Exact
          </span>
        )}
      </span>
      {result.snippet && (
        <pre className="max-h-32 overflow-hidden rounded bg-zinc-50 px-3 py-2 font-mono text-xs whitespace-pre-wrap break-words text-zinc-700 dark:bg-zinc-900 dark:text-zinc-300">
          {result.snippet}
        </pre>
      )}
    </Link>
  );
}
