"use client";

import { useCallback, useContext, useState } from "react";
import PageViewer from "@/components/PageViewer";
import { SourcePanelContext } from "@/lib/sourcePanel";
import { isViewable, sourceKey } from "@/lib/sources";
import type { ViewableSource } from "@/lib/sources";
import type { Source } from "@/lib/types";

function DocumentIcon() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 20 20"
      fill="currentColor"
      className="h-4 w-4 shrink-0"
    >
      <path d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4z" />
    </svg>
  );
}

interface SourceListProps {
  sources: Source[];
  /** The answer these sources belong to; with it, a source can be shown in the page panel. */
  messageId?: string;
}

export default function SourceList({ sources, messageId }: SourceListProps) {
  const [viewing, setViewing] = useState<ViewableSource | null>(null);
  const close = useCallback(() => setViewing(null), []);
  const panel = useContext(SourcePanelContext);
  // On a wide window the page opens in the panel on the right; elsewhere, in a pop-up.
  const usePanel = Boolean(panel?.enabled && messageId);
  const selected = usePanel ? panel?.selected : null;

  return (
    <div className="mt-3 border-t border-black/5 pt-3">
      <p className="mb-2 text-sm font-medium text-muted">Sources</p>
      <ul className="flex flex-wrap gap-2">
        {sources.map((source, index) => {
          const shown =
            selected != null &&
            isViewable(source) &&
            selected.messageId === messageId &&
            selected.key === sourceKey(source);
          return (
            <li
              key={`${source.filename}-${source.page_number ?? "none"}-${index}`}
              className={`inline-flex max-w-full items-center gap-1.5 rounded-full bg-gray-100 px-3.5 py-1.5 text-sm text-primary ${
                shown ? "ring-2 ring-accent" : ""
              }`}
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
              {isViewable(source) && (
                <button
                  type="button"
                  onClick={() => (usePanel ? panel!.select(messageId!, source) : setViewing(source))}
                  aria-label={`View page ${source.page_number} of ${source.filename}`}
                  aria-pressed={usePanel ? shown : undefined}
                  className="-mr-1.5 shrink-0 rounded-full bg-primary px-3 py-0.5 text-sm font-medium text-white transition hover:bg-primary/90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
                >
                  View page
                </button>
              )}
            </li>
          );
        })}
      </ul>
      {viewing && (
        <PageViewer
          filename={viewing.filename}
          documentId={viewing.document_id}
          pageNumber={viewing.page_number}
          onClose={close}
        />
      )}
    </div>
  );
}
