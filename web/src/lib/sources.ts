// The sources an answer was taken from, as the short lines of the collapsed "Sources" list under it (no page viewer: the pages are named, not shown).
// Pure, so it is tested directly.
import type { Source } from "./types";

export const MAX_SOURCES = 8;

export interface SourceLine {
  key: string;
  /** "1949 April · page 87" */
  label: string;
}

/** The file name as a person reads it: without ".PDF" and with spaces for underscores. */
function readableName(filename: string): string {
  return filename.replace(/\.pdf$/i, "").replace(/_/g, " ").trim();
}

/** One line per page found (the same page once, the best match first), at most MAX_SOURCES. Anything without a file name is left out. */
export function sourceLines(sources: Source[] | undefined): SourceLine[] {
  const seen = new Set<string>();
  const lines: SourceLine[] = [];
  for (const source of Array.isArray(sources) ? sources : []) {
    if (!source || typeof source.filename !== "string" || !source.filename.trim()) continue;
    const page = Number.isInteger(source.page_number) && (source.page_number as number) >= 1 ? (source.page_number as number) : null;
    const key = `${source.filename}:${page ?? ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const name = readableName(source.filename);
    lines.push({ key, label: page === null ? name : `${name} · page ${page}` });
    if (lines.length === MAX_SOURCES) break;
  }
  return lines;
}
