"use client";

import { useState } from "react";
import { pageImageUrl } from "@/lib/api";

interface PageImageProps {
  filename: string;
  documentId: number;
  pageNumber: number;
}

type Status = "loading" | "ready" | "error";

/**
 * One scanned page as an image, with its loading and failure states. It is shared by the
 * pop-up (PageViewer) and the page panel. Give it a new `key` to show another page.
 */
export default function PageImage({ filename, documentId, pageNumber }: PageImageProps) {
  const [status, setStatus] = useState<Status>("loading");
  const [attempt, setAttempt] = useState(0);

  // A retry asks for the page again under a new address, so a failed answer is not reused.
  const src = pageImageUrl(documentId, pageNumber) + (attempt > 0 ? `?retry=${attempt}` : "");

  function retry() {
    setStatus("loading");
    setAttempt((n) => n + 1);
  }

  return (
    <>
      {status === "loading" && (
        <p role="status" className="py-16 text-center text-base text-muted">
          Loading the page... The first page of an issue can take a few seconds.
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
    </>
  );
}
