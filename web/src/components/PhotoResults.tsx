"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { pageImageUrl } from "@/lib/api";
import { photoAlt, photoImageUrl, photoSourceLabel } from "@/lib/photos";
import type { Photo } from "@/lib/types";

/** The photographs found for a question, as a strip of thumbnails under the answer. Clicking one shows it large. */
export default function PhotoResults({ photos }: { photos: Photo[] }) {
  const [open, setOpen] = useState<Photo | null>(null);
  // A picture that cannot be loaded (removed, or the server was busy) is left out rather than shown as a broken box.
  const [broken, setBroken] = useState<Set<number>>(new Set());
  const opener = useRef<HTMLElement | null>(null);

  const shown = photos.filter((photo) => !broken.has(photo.id));
  const close = useCallback(() => {
    setOpen(null);
    opener.current?.focus();
  }, []);

  if (shown.length === 0) return null;
  return (
    <section aria-label="Photographs from the archive" className="mt-4">
      <h3 className="text-sm font-medium text-muted">Photographs from the archive</h3>
      <ul className="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-3">
        {shown.map((photo) => (
          <li key={photo.id} className="min-w-0">
            <button
              type="button"
              onClick={(event) => {
                opener.current = event.currentTarget;
                setOpen(photo);
              }}
              aria-label={`Enlarge: ${photoAlt(photo)}`}
              className="block w-full overflow-hidden rounded-xl bg-black/5 ring-1 ring-black/10 transition hover:ring-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
            >
              {/* A plain <img>: the picture comes from this app's own route, which is dynamic. */}
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={photoImageUrl(photo.id)}
                alt={photoAlt(photo)}
                loading="lazy"
                onError={() => setBroken((previous) => new Set(previous).add(photo.id))}
                className="h-40 w-full object-contain"
              />
            </button>
            <p className="mt-1 line-clamp-2 break-words text-sm text-ink">{photo.caption || "No caption"}</p>
            <p className="text-xs text-muted">{photoSourceLabel(photo)}</p>
          </li>
        ))}
      </ul>
      {open && <Lightbox photo={open} onClose={close} />}
    </section>
  );
}

/** One photograph shown large, with its caption and where it comes from. Escape or a click outside closes it. */
function Lightbox({ photo, onClose }: { photo: Photo; onClose: () => void }) {
  const closeButton = useRef<HTMLButtonElement>(null);
  const dialog = useRef<HTMLDivElement>(null);

  useEffect(() => {
    closeButton.current?.focus();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden"; // the page behind does not scroll
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, []);

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    // Keep Tab inside the dialog: it has just two things to reach.
    const items = dialog.current?.querySelectorAll<HTMLElement>("a[href], button");
    if (!items || items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
      onClick={onClose}
      onKeyDown={onKeyDown}
      role="presentation"
    >
      <div
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-label={photoAlt(photo)}
        onClick={(event) => event.stopPropagation()}
        className="flex max-h-full w-full max-w-4xl flex-col gap-3 rounded-2xl bg-surface p-4 shadow-xl"
      >
        <div className="flex justify-end">
          <button
            ref={closeButton}
            type="button"
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-base font-medium text-primary ring-1 ring-primary/20 hover:bg-primary/5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
          >
            Close
          </button>
        </div>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={photoImageUrl(photo.id)} alt={photoAlt(photo)} className="mx-auto max-h-[70vh] max-w-full min-h-0 object-contain" />
        <div>
          {photo.caption && <p className="break-words text-base text-ink md:text-lg">{photo.caption}</p>}
          <p className="text-sm text-muted">
            {photoSourceLabel(photo)} ·{" "}
            <a
              href={pageImageUrl(photo.document_id, photo.page_number)}
              target="_blank"
              rel="noopener noreferrer"
              className="text-primary underline underline-offset-2 hover:text-accent"
            >
              Open the scanned page
            </a>
          </p>
        </div>
      </div>
    </div>
  );
}
