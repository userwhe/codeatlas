"use client";

/** The processing disclosure for private repositories (FR-006), covering pull request content. */
export const EXTERNAL_PROCESSING_DISCLOSURE =
  "Selected source excerpts, pull request descriptions, and pull request changes may be sent to an external model provider";

/**
 * The checkbox that accepts the external processing disclosure for a private repository: when
 * connecting it, and when re-indexing a repository that became private (FR-017).
 */
export function ExternalProcessingAcceptance({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100">
      <input
        type="checkbox"
        required
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="mt-0.5"
      />
      <span>{EXTERNAL_PROCESSING_DISCLOSURE}. I accept this for this private repository.</span>
    </label>
  );
}
