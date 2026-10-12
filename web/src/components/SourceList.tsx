import type { Source } from "@/lib/types";

function DocumentIcon() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 20 20"
      fill="currentColor"
      className="h-3.5 w-3.5 shrink-0"
    >
      <path d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4z" />
    </svg>
  );
}

export default function SourceList({ sources }: { sources: Source[] }) {
  return (
    <div className="mt-3 border-t border-black/5 pt-3">
      <p className="mb-2 text-xs font-medium text-muted">Sources</p>
      <ul className="flex flex-wrap gap-2">
        {sources.map((source, index) => (
          <li
            key={`${source.filename}-${source.page_number ?? "none"}-${index}`}
            className="inline-flex max-w-full items-center gap-1.5 rounded-full bg-gray-100 px-3 py-1 text-xs text-primary"
          >
            <DocumentIcon />
            <span className="max-w-[14rem] truncate" title={source.filename}>
              {source.filename}
            </span>
            {source.page_number != null && (
              <span className="shrink-0 font-medium">p.{source.page_number}</span>
            )}
            <span className="shrink-0 text-primary/70">
              {(source.similarity * 100).toFixed(0)}% match
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
