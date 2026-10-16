"use client";

import { pageImageUrl } from "@/lib/api";
import { sourceKey } from "@/lib/sources";
import type { ViewableSource } from "@/lib/sources";
import PageImage from "./PageImage";

interface PagePanelProps {
  /** The pages cited by one answer, best match first. */
  sources: ViewableSource[];
  selectedKey: string;
  onSelect: (source: ViewableSource) => void;
  onClose: () => void;
}

/** A sidebar on the right showing the scanned pages an answer was taken from. */
export default function PagePanel({ sources, selectedKey, onSelect, onClose }: PagePanelProps) {
  const selected = sources.find((source) => sourceKey(source) === selectedKey) ?? sources[0];

  return (
    <aside
      aria-label="Scanned pages"
      className="fixed inset-y-0 right-0 z-20 hidden w-[26rem] flex-col border-l border-black/10 bg-surface xl:flex"
    >
      <div className="flex items-center gap-2 border-b border-black/5 px-4 py-3">
        <h2 className="min-w-0 flex-1 text-base font-semibold text-primary">Scanned pages</h2>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close page panel"
          className="shrink-0 rounded-lg p-1.5 text-primary transition hover:bg-primary/5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
        >
          <svg aria-hidden="true" viewBox="0 0 20 20" fill="currentColor" className="h-5 w-5">
            <path d="M5.28 4.22a.75.75 0 00-1.06 1.06L8.94 10l-4.72 4.72a.75.75 0 101.06 1.06L10 11.06l4.72 4.72a.75.75 0 101.06-1.06L11.06 10l4.72-4.72a.75.75 0 00-1.06-1.06L10 8.94 5.28 4.22z" />
          </svg>
        </button>
      </div>

      <ul aria-label="Pages cited in the answer" className="flex max-h-40 shrink-0 flex-wrap gap-1.5 overflow-y-auto border-b border-black/5 px-4 py-3">
        {sources.map((source) => {
          const key = sourceKey(source);
          const active = key === selectedKey;
          return (
            <li key={key} className="max-w-full">
              <button
                type="button"
                onClick={() => onSelect(source)}
                aria-pressed={active}
                title={`${source.filename}, page ${source.page_number}`}
                className={`inline-flex max-w-full items-center gap-1.5 rounded-full px-3 py-1 text-sm transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary ${
                  active ? "bg-primary text-white" : "bg-gray-100 text-primary hover:bg-gray-200"
                }`}
              >
                <span className="max-w-[9rem] truncate">{source.filename}</span>
                <span className="shrink-0 font-medium">p.{source.page_number}</span>
              </button>
            </li>
          );
        })}
      </ul>

      <div className="flex items-center gap-2 px-4 py-2.5">
        <p className="flex min-w-0 flex-1 items-baseline gap-1.5 text-sm text-primary" title={`${selected.filename}, page ${selected.page_number}`}>
          <span className="shrink-0 font-semibold">Page {selected.page_number}</span>
          <span className="min-w-0 truncate text-muted">{selected.filename}</span>
        </p>
        <a
          href={pageImageUrl(selected.document_id, selected.page_number)}
          target="_blank"
          rel="noopener noreferrer"
          className="shrink-0 rounded-lg px-3 py-1.5 text-sm font-medium text-primary ring-1 ring-primary/30 transition hover:bg-primary/5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
        >
          Open in new tab
        </a>
      </div>

      <div className="min-h-0 flex-1 overflow-auto bg-gray-100 p-3">
        {/* A new key per page: switching sources starts the new image from "loading". */}
        <PageImage
          key={selectedKey}
          filename={selected.filename}
          documentId={selected.document_id}
          pageNumber={selected.page_number}
        />
      </div>
    </aside>
  );
}
