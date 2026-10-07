import { CodeLines } from "@/components/CodeView";
import { shortSha } from "@/lib/api/repositories";
import { externalUrl, type ReviewCitation as Citation } from "@/lib/api/reviews";

/** "line 4" or "lines 4–9". */
export function citationLines({ start_line, end_line }: Citation) {
  return start_line === end_line ? `line ${start_line}` : `lines ${start_line}–${end_line}`;
}

/** Which side of the change the cited lines are on, and that side's commit. */
export function sideLabel(citation: Citation) {
  const sha = shortSha(citation.commit_sha);
  return citation.side === "before" ? `Before · merge base ${sha}` : `After · head ${sha}`;
}

const SIDE_STYLES: Record<Citation["side"], string> = {
  before: "bg-rose-50 text-rose-800 dark:bg-rose-950 dark:text-rose-200",
  after: "bg-emerald-50 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
};

// Context that is not part of the change is a candidate, not a complete set (FR-012).
const SOURCE_LABELS: Partial<Record<Citation["source_type"], string>> = {
  reference: "Related code (candidate, found by name)",
  test: "Candidate test (not run by CodeAtlas)",
};

/**
 * The excerpt's lines, one per cited line. Review excerpts join the cited lines with "\n" and
 * have no final line break, so a last cited line that is blank is kept. Only an empty element
 * beyond the cited range, after a final line break, is dropped.
 */
function excerptLines({ excerpt, start_line, end_line }: Citation) {
  const lines = excerpt.split("\n");
  if (lines.length > end_line - start_line + 1 && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

/**
 * One cited range of a review: its label, side, path, and lines, a link to the lines on GitHub
 * at the cited commit, and the excerpt, shown when expanded. With `anchor`, the item has the id
 * `citation-<label>` so that label links can jump to it.
 */
export function ReviewCitation({
  citation,
  open,
  onToggle,
  anchor = false,
  idPrefix = "citation",
}: {
  citation: Citation;
  open: boolean;
  onToggle: () => void;
  anchor?: boolean;
  idPrefix?: string;
}) {
  const excerptId = `${idPrefix}-${citation.label}-excerpt`;
  const githubUrl = externalUrl(citation.github_url);
  const sourceLabel = SOURCE_LABELS[citation.source_type];
  return (
    <li
      id={anchor ? `citation-${citation.label}` : undefined}
      tabIndex={anchor ? -1 : undefined}
      className="scroll-mt-24 overflow-hidden rounded-lg border border-zinc-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-zinc-800"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3">
        <span className="rounded bg-blue-50 px-1.5 py-0.5 font-mono text-xs font-medium text-blue-800 dark:bg-blue-950 dark:text-blue-200">
          {citation.label}
        </span>
        <span
          className={`rounded px-1.5 py-0.5 text-xs font-medium ${SIDE_STYLES[citation.side]}`}
          title={citation.commit_sha}
        >
          {sideLabel(citation)}
        </span>
        {sourceLabel && (
          <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-xs font-medium text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">
            {sourceLabel}
          </span>
        )}
        <span className="min-w-0 font-mono text-sm break-all">{citation.path}</span>
        <span className="text-xs text-zinc-500">{citationLines(citation)}</span>
        <span className="ml-auto flex items-center gap-4 text-sm">
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={open}
            aria-controls={excerptId}
            className="font-medium underline"
          >
            {open ? "Hide excerpt" : "Show excerpt"}
          </button>
          {githubUrl && (
            <a href={githubUrl} target="_blank" rel="noreferrer" className="font-medium underline">
              View on GitHub
            </a>
          )}
        </span>
      </div>
      <div
        id={excerptId}
        hidden={!open}
        className="border-t border-zinc-200 py-2 dark:border-zinc-800"
      >
        <p className="px-3 pb-2 text-xs text-zinc-500">{sideLabel(citation)}</p>
        <CodeLines lines={excerptLines(citation)} firstLine={citation.start_line} />
      </div>
    </li>
  );
}
