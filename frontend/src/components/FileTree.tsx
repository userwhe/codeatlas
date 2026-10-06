"use client";

import Link from "next/link";
import { useState } from "react";

import { browseUrl, useTree } from "@/lib/api/browse";
import { errorMessage } from "@/lib/api/client";

interface LevelProps {
  snapshotId: string;
  selectedPath: string | null;
  depth: number;
}

function indent(depth: number) {
  return { paddingLeft: `${0.5 + depth * 0.75}rem` };
}

function childPath(parent: string, name: string) {
  return parent ? `${parent}/${name}` : name;
}

/**
 * The file tree of a snapshot. Directories load their children when first expanded; the
 * directories that contain `selectedPath` start expanded, and file links open the file.
 */
export function FileTree({
  snapshotId,
  selectedPath,
}: {
  snapshotId: string;
  selectedPath: string | null;
}) {
  return (
    <nav aria-label="Files" className="text-sm">
      <TreeLevel path="" snapshotId={snapshotId} selectedPath={selectedPath} depth={0} />
    </nav>
  );
}

function TreeLevel({ path, ...props }: LevelProps & { path: string }) {
  const { snapshotId, selectedPath, depth } = props;
  const tree = useTree(snapshotId, path);

  if (!tree.data) {
    return tree.error ? (
      <p role="alert" style={indent(depth)} className="py-1 text-red-700 dark:text-red-400">
        Could not load {path || "the files"}: {errorMessage(tree.error)}
      </p>
    ) : (
      <p style={indent(depth)} className="py-1 text-zinc-500">
        Loading…
      </p>
    );
  }

  if (tree.data.entries.length === 0) {
    return (
      <p style={indent(depth)} className="py-1 text-zinc-500">
        No files.
      </p>
    );
  }

  return (
    <ul>
      {tree.data.entries.map((entry) => {
        const entryPath = childPath(path, entry.name);
        if (entry.type === "directory") {
          return <DirectoryNode key={entry.name} path={entryPath} name={entry.name} {...props} />;
        }
        const selected = entryPath === selectedPath;
        return (
          <li key={entry.name}>
            <Link
              href={browseUrl(snapshotId, entryPath)}
              aria-current={selected ? "page" : undefined}
              title={entryPath}
              style={indent(depth)}
              className={`block truncate rounded py-0.5 pr-2 ${
                selected
                  ? "bg-zinc-200 font-medium dark:bg-zinc-800"
                  : "hover:bg-zinc-100 dark:hover:bg-zinc-900"
              }`}
            >
              <span aria-hidden="true" className="mr-1.5 inline-block w-3" />
              {entry.name}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

function DirectoryNode({
  path,
  name,
  ...props
}: LevelProps & { path: string; name: string }) {
  const [expanded, setExpanded] = useState(
    () => props.selectedPath?.startsWith(`${path}/`) ?? false,
  );
  return (
    <li>
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
        title={path}
        style={indent(props.depth)}
        className="block w-full truncate rounded py-0.5 pr-2 text-left hover:bg-zinc-100 dark:hover:bg-zinc-900"
      >
        <span aria-hidden="true" className="mr-1.5 inline-block w-3 text-zinc-500">
          {expanded ? "▾" : "▸"}
        </span>
        {name}
        <span className="sr-only"> (folder)</span>
      </button>
      {expanded && <TreeLevel {...props} path={path} depth={props.depth + 1} />}
    </li>
  );
}
