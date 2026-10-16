import type { AuditResponse, ChatResponse, DocumentsResponse } from "./types";

// Every call goes to this app's own /api routes (src/app/api), which forward it to
// the backend. The browser never talks to the backend directly, so the page works on
// any port and the backend's CORS list does not matter.

async function failWith(res: Response): Promise<never> {
  const err = await res.json().catch(() => ({}));
  throw new Error(err.detail ?? `Server error ${res.status}`);
}

export async function sendChat(
  query: string,
  top_k?: number
): Promise<ChatResponse> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // top_k is left out unless given, so the backend's default applies.
    body: JSON.stringify(top_k === undefined ? { query } : { query, top_k }),
  });
  if (!res.ok) await failWith(res);
  return res.json();
}

export async function getIngestStatus() {
  const res = await fetch("/api/ingest/status");
  if (!res.ok) await failWith(res);
  return res.json();
}

export async function triggerSync() {
  const res = await fetch("/api/ingest/sync", { method: "POST" });
  if (!res.ok) await failWith(res);
  return res.json();
}

export async function getDocuments(): Promise<DocumentsResponse> {
  const res = await fetch("/api/documents");
  if (!res.ok) await failWith(res);
  return res.json();
}

/** The address of a page of a document as an image (a source's document_id and page_number). */
export function pageImageUrl(documentId: number, pageNumber: number): string {
  return `/api/documents/${documentId}/pages/${pageNumber}`;
}

export async function getAuditLog(
  options: { limit?: number; offset?: number; event?: "chat" | "sync" } = {}
): Promise<AuditResponse> {
  const params = new URLSearchParams();
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined) params.set("offset", String(options.offset));
  if (options.event) params.set("event", options.event);
  const query = params.toString();
  const res = await fetch("/api/audit" + (query ? "?" + query : ""));
  if (!res.ok) await failWith(res);
  return res.json();
}
