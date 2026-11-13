import { sourceLines } from "@/lib/sources";
import type { Source } from "@/lib/types";

/**
 * The pages an answer was taken from, in a small section that is closed until opened (a plain <details>, so the keyboard and screen readers
 * get it for nothing). The pages are named, not shown: there is no page viewer.
 */
export default function Sources({ sources }: { sources: Source[] | undefined }) {
  const lines = sourceLines(sources);
  if (lines.length === 0) return null;
  return (
    <details className="group mt-4 border-t border-line pt-3 text-sm">
      <summary className="flex w-fit cursor-pointer select-none list-none items-center gap-1.5 rounded-md py-0.5 pr-2 font-medium text-muted transition-colors hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent [&::-webkit-details-marker]:hidden">
        <svg aria-hidden="true" viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4 text-azure transition-transform duration-200 group-open:rotate-90">
          <path d="M7.2 4.6a1 1 0 011.4 0l4.7 4.7a1 1 0 010 1.4l-4.7 4.7a1 1 0 01-1.4-1.4L11.2 10 7.2 6a1 1 0 010-1.4z" />
        </svg>
        Sources ({lines.length})
      </summary>
      <ul className="mt-2 animate-fade-in space-y-1.5 text-muted">
        {lines.map((line) => (
          <li key={line.key} className="flex items-start gap-2 break-words">
            <svg aria-hidden="true" viewBox="0 0 20 20" fill="currentColor" className="mt-0.5 h-3.5 w-3.5 shrink-0 text-azure">
              <path d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4z" />
            </svg>
            <span>{line.label}</span>
          </li>
        ))}
      </ul>
    </details>
  );
}
