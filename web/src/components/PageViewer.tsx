"use client";

import { useEffect, useId, useRef, useState } from "react";
import { pageImageUrl } from "@/lib/api";

interface PageViewerProps {
  filename: string;
  documentId: number;
  pageNumber: number;
  onClose: () => void;
}

type Status = "loading" | "ready" | "error";

/** A window showing one page of a magazine as it was printed. */
export default function PageViewer({ filename, documentId, pageNumber, onClose }: PageViewerProps) {
  const [status, setStatus] = useState<Status>("loading");
  const [attempt, setAttempt] = useState(0);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const titleId = useId();

  // A retry asks for the page again under a new address, so a failed answer is not reused.
  const src = pageImageUrl(documentId, pageNumber) + (attempt > 0 ? `?retry=${attempt}` : "");

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

  function retry() {
    setStatus("loading");
    setAttempt((n) => n + 1);
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
            href={src}
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
          {status === "loading" && (
            <p role="status" className="py-16 text-center text-base text-muted">
              Loading the page... The first page of an issue can take up to a minute.
            </p>
          )}
          {status === "error" && (
            <div role="alert" className="py-16 text-center">
              <p className="text-base text-primary">Could not load this page.</p>
              <button
                type="button"
                onClick={retry}
                className="mt-3 rounded-lg bg-accent px-5 py-2.5 text-sm font-semibold text-ink transition hover:brightness-110 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
              >
                Try again
              </button>
            </div>
          )}
          {/* A plain img: the page is already a finished JPEG served by our own route. */}
          <img
            key={attempt}
            src={src}
            alt={`${filename}, page ${pageNumber}`}
            onLoad={() => setStatus("ready")}
            onError={() => setStatus("error")}
            className={status === "ready" ? "mx-auto h-auto w-full max-w-3xl rounded bg-white shadow" : "hidden"}
          />
        </div>
      </div>
    </div>
  );
}
