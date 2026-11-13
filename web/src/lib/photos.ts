// Photographs the backend found for a question (see backend/photos/search.py): checking what arrives, and how they are shown. Pure, so it is tested directly.
import type { Photo } from "./types";

export const MAX_PHOTOS = 8;
const KINDS = ["photo", "advertisement", "illustration"] as const;
const MAX_CAPTION_CHARS = 300;

const isWhole = (value: unknown): value is number => typeof value === "number" && Number.isInteger(value) && value >= 1;

/** Whatever came as `photos` (from the server, or from a chat saved in this browser), reduced to well-formed photographs: at most MAX_PHOTOS, each id once. */
export function cleanPhotos(value: unknown): Photo[] {
  if (!Array.isArray(value)) return [];
  const photos: Photo[] = [];
  const seen = new Set<number>();
  for (const item of value) {
    if (typeof item !== "object" || item === null) continue;
    const p = item as Record<string, unknown>;
    if (!isWhole(p.id) || !isWhole(p.document_id) || !isWhole(p.page_number) || seen.has(p.id)) continue;
    seen.add(p.id);
    photos.push({
      id: p.id,
      document_id: p.document_id,
      page_number: p.page_number,
      filename: typeof p.filename === "string" ? p.filename : "",
      year: typeof p.year === "string" && p.year ? p.year : null,
      caption: typeof p.caption === "string" ? p.caption.replace(/\s+/g, " ").trim().slice(0, MAX_CAPTION_CHARS) : "",
      kind: (KINDS as readonly unknown[]).includes(p.kind) ? (p.kind as Photo["kind"]) : "photo",
      contains_face: p.contains_face === true,
    });
    if (photos.length === MAX_PHOTOS) break;
  }
  return photos;
}

/** The picture of a photograph. It goes through this app's own route (which asks the backend), so it never needs a link that expires and a saved chat keeps its pictures. */
export const photoImageUrl = (id: number): string => `/api/photos/${id}`;

/** Words for a screen reader and for the picture's alternative text: the caption, or where the photograph is from. */
export function photoAlt(photo: Photo): string {
  return photo.caption || `Photograph on page ${photo.page_number} of Sampada${photo.year ? ` ${photo.year}` : ""}`;
}

/** Where a photograph comes from, as a line under it: "Page 214 · Sampada 1961-62". */
export function photoSourceLabel(photo: Photo): string {
  return `Page ${photo.page_number}${photo.year ? ` · Sampada ${photo.year}` : ""}`;
}
