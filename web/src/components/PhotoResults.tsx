"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { pageImageUrl } from "@/lib/api";
import { PHOTOS_RELATED_HEADING, photoAlt, photoImageUrl, photoSourceLabel } from "@/lib/photos";
import type { Photo } from "@/lib/types";

/**
 * The photographs found for a question, as a strip of thumbnails under the answer. Clicking one shows it large.
 * `showHeading` is false when the message already introduces them itself; `onAllBroken` is called when none of them could be loaded.
 */
export default function PhotoResults({
  photos,
  showHeading = true,
  onAllBroken,
}: {
  photos: Photo[];
  showHeading?: boolean;
  onAllBroken?: () => void;
}) {
  const [open, setOpen] = useState<Photo | null>(null);
  // A picture that cannot be loaded (removed, or the server was busy) is left out rather than shown as a broken box.
  const [broken, setBroken] = useState<Set<number>>(new Set());
  const opener = useRef<HTMLElement | null>(null);

  const shown = photos.filter((photo) => !broken.has(photo.id));
  const close = useCallback(() => {
    setOpen(null);
    opener.current?.focus();
  }, []);
  const noneLeft = photos.length > 0 && shown.length === 0;
  useEffect(() => {
    if (noneLeft) onAllBroken?.();
  }, [noneLeft, onAllBroken]);

  if (shown.length === 0) return null;
  return (
    <section aria-label="Photographs from the archive" className={showHeading ? "mt-5 border-t border-line pt-4" : "mt-4"}>
      {showHeading && <h3 className="text-sm font-semibold text-ink">{PHOTOS_RELATED_HEADING}</h3>}
      {/* 1 column on a phone, 2 on a tablet, 3 on a desktop; every picture sits in the same 4:3 frame, whole (never cropped: these are old scans). */}
      <ul className={`${showHeading ? "mt-3 " : ""}grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3`}>
        {shown.map((photo) => (
          <li key={photo.id} className="min-w-0">
            {/* The card lifts on hover; the picture inside zooms a little. */}
            <div className="group lift flex h-full flex-col overflow-hidden rounded-lg border border-line bg-white shadow-card">
              <button
                type="button"
                onClick={(event) => {
                  opener.current = event.currentTarget;
                  setOpen(photo);
                }}
                aria-label={`Enlarge: ${photoAlt(photo)}`}
                className="block w-full overflow-hidden bg-sidebar focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent"
              >
                <span className="block aspect-[4/3] w-full overflow-hidden">
                  {/* A plain <img>: the picture comes from this app's own route, which is dynamic. */}
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={photoImageUrl(photo.id)}
                    alt={photoAlt(photo)}
                    loading="lazy"
                    onError={() => setBroken((previous) => new Set(previous).add(photo.id))}
                    className="h-full w-full object-contain transition-transform duration-200 ease-out group-hover:scale-[1.03] motion-reduce:group-hover:scale-100"
                  />
                </span>
              </button>
              <div className="px-3 py-2.5">
                <p className="line-clamp-2 break-words text-sm leading-snug text-ink">{photo.caption || "No caption"}</p>
                <p className="mt-0.5 text-xs text-muted">{photoSourceLabel(photo)}</p>
              </div>
            </div>
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

  // In a portal on <body>, so no ancestor (a message that is animating, a panel with a filter) can clip or offset it.
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex animate-fade-in items-center justify-center bg-black/75 p-4"
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
        className="flex max-h-full w-full max-w-5xl animate-scale-in flex-col gap-3 rounded-2xl bg-white p-4 shadow-2xl"
      >
        <div className="flex justify-end">
          <button
            ref={closeButton}
            type="button"
            onClick={onClose}
            className="flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-sm font-medium text-ink ring-1 ring-line transition hover:bg-tint focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            <svg aria-hidden="true" viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4">
              <path d="M5.28 4.22a.75.75 0 00-1.06 1.06L8.94 10l-4.72 4.72a.75.75 0 101.06 1.06L10 11.06l4.72 4.72a.75.75 0 101.06-1.06L11.06 10l4.72-4.72a.75.75 0 00-1.06-1.06L10 8.94 5.28 4.22z" />
            </svg>
            Close
          </button>
        </div>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={photoImageUrl(photo.id)} alt={photoAlt(photo)} className="mx-auto max-h-[70vh] min-h-0 max-w-full rounded-xl bg-sidebar object-contain" />
        <div>
          {photo.caption && <p className="break-words text-base text-ink md:text-lg">{photo.caption}</p>}
          <p className="mt-0.5 text-sm text-muted">
            {photoSourceLabel(photo)} ·{" "}
            <a
              href={pageImageUrl(photo.document_id, photo.page_number)}
              target="_blank"
              rel="noopener noreferrer"
              className="text-accent underline underline-offset-2 hover:text-accent-dark"
            >
              Open the scanned page
            </a>
          </p>
        </div>
      </div>
    </div>,
    document.body
  );
}
