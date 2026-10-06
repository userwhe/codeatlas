"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

import { MAX_QUESTION_CHARS, questionErrorMessage, useAskQuestion } from "@/lib/api/questions";

const NO_READY_VERSION = "This repository has no ready version yet.";

/**
 * Asks a question about the repository's active indexed version and opens the answer page.
 * Disabled, with the reason, while the repository has no ready version.
 */
export function AskQuestionForm({
  repositoryId,
  hasReadyVersion,
}: {
  repositoryId: string;
  hasReadyVersion: boolean;
}) {
  const router = useRouter();
  const ask = useAskQuestion();
  const [question, setQuestion] = useState("");

  const trimmed = question.trim();
  const busy = ask.isPending || ask.isSuccess;
  const disabled = !hasReadyVersion || busy;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (disabled || trimmed.length === 0) return;
    ask.mutate(
      {
        body: { repository_id: repositoryId, kind: "repository_qa", question: trimmed },
        // A new key for each submission attempt; a replay of the same request queues it once.
        idempotencyKey: crypto.randomUUID(),
      },
      { onSuccess: ({ run_id }) => router.push(`/answers/${run_id}`) },
    );
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-2">
      <label htmlFor="question" className="text-sm font-medium">
        Ask a question
      </label>
      <textarea
        id="question"
        value={question}
        onChange={(event) => setQuestion(event.target.value)}
        disabled={disabled}
        required
        maxLength={MAX_QUESTION_CHARS}
        rows={3}
        placeholder="Where are repository permissions checked?"
        aria-describedby="question-help question-count"
        className="rounded-md border border-zinc-300 bg-transparent px-3 py-2 text-sm disabled:opacity-60 dark:border-zinc-700"
      />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p id="question-help" className="text-xs text-zinc-500">
          {hasReadyVersion
            ? "Answers use the active indexed version and cite the exact lines they rely on."
            : NO_READY_VERSION}
        </p>
        <div className="flex items-center gap-3">
          <span id="question-count" className="text-xs text-zinc-500 tabular-nums">
            {question.length.toLocaleString()} / {MAX_QUESTION_CHARS.toLocaleString()}
            <span className="sr-only"> characters</span>
          </span>
          <button
            type="submit"
            disabled={disabled || trimmed.length === 0}
            className="rounded-md bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
          >
            {busy ? "Asking…" : "Ask"}
          </button>
        </div>
      </div>
      {ask.error && (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          {questionErrorMessage(ask.error)}
        </p>
      )}
    </form>
  );
}
