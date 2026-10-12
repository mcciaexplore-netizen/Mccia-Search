import type { ChatResponse } from "./types";

const BACKEND = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000";

export async function sendChat(
  query: string,
  top_k = 5
): Promise<ChatResponse> {
  const res = await fetch(`${BACKEND}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, top_k }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail ?? `Server error ${res.status}`);
  }
  return res.json();
}

export async function getIngestStatus() {
  const res = await fetch(`${BACKEND}/ingest/status`);
  return res.json();
}

export async function triggerSync() {
  const res = await fetch(`${BACKEND}/ingest/sync`, { method: "POST" });
  return res.json();
}
