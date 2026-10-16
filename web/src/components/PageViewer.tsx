"use client";

import { useEffect, useId, useRef } from "react";
import { pageImageUrl } from "@/lib/api";
import PageImage from "./PageImage";

interface PageViewerProps {
  filename: string;
  documentId: number;
  pageNumber: number;
  onClose: () => void;
}

/** A pop-up window showing one page of a magazine as it was printed. */
export default function PageViewer({ filename, documentId, pageNumber, onClose }: PageViewerProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const titleId = useId();

  // While open: Escape closes, the page behind does not scroll, and focus starts on
  // Close and goes back to the button that opened the window.
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCloseRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
      opener?.focus();
    };
  }, []);

  // Tab stays inside the window.
  function keepFocusInside(event: React.KeyboardEvent) {
    if (event.key !== "Tab" || !dialogRef.current) return;
    const focusable = dialogRef.current.querySelectorAll<HTMLElement>("a[href], button:not([disabled])");
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-3 sm:p-6"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={keepFocusInside}
        className="flex max-h-full w-full max-w-4xl flex-col overflow-hidden rounded-2xl bg-surface shadow-xl"
      >
        <div className="flex items-center gap-3 border-b border-black/5 px-4 py-3">
          <h2 id={titleId} className="min-w-0 flex-1 truncate text-base font-semibold text-primary" title={`${filename}, page ${pageNumber}`}>
            {filename} <span className="font-normal text-muted">· page {pageNumber}</span>
          </h2>
          <a
            href={pageImageUrl(documentId, pageNumber)}
            target="_blank"
            rel="noopener noreferrer"
            className="shrink-0 rounded-lg px-3 py-1.5 text-sm font-medium text-primary ring-1 ring-primary/30 transition hover:bg-primary/5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
          >
            Open in new tab
          </a>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="shrink-0 rounded-lg p-1.5 text-primary transition hover:bg-primary/5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
          >
            <svg aria-hidden="true" viewBox="0 0 20 20" fill="currentColor" className="h-5 w-5">
              <path d="M5.28 4.22a.75.75 0 00-1.06 1.06L8.94 10l-4.72 4.72a.75.75 0 101.06 1.06L10 11.06l4.72 4.72a.75.75 0 101.06-1.06L11.06 10l4.72-4.72a.75.75 0 00-1.06-1.06L10 8.94 5.28 4.22z" />
            </svg>
          </button>
        </div>

        <div className="overflow-auto bg-gray-100 p-3 sm:p-4">
          <PageImage filename={filename} documentId={documentId} pageNumber={pageNumber} />
        </div>
      </div>
    </div>
  );
}
