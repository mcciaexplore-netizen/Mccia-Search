import { NextRequest, NextResponse } from "next/server";
import { clientAddress, proxyToBackend } from "@/lib/proxy";

export async function POST(req: NextRequest) {
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ detail: "Request body must be JSON." }, { status: 400 });
  }
  const { query, top_k } = (typeof body === "object" && body !== null ? body : {}) as Record<string, unknown>;
  if (typeof query !== "string" || !query.trim() || query.length > 300) {
    return NextResponse.json({ detail: "query must be a non-empty text of at most 300 characters." }, { status: 400 });
  }
  // Only what the backend expects is passed on.
  const payload = { query, ...(typeof top_k === "number" && Number.isInteger(top_k) && top_k >= 1 && top_k <= 12 ? { top_k } : {}) };
  return proxyToBackend("/search/photos", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...clientAddress(req) },
    body: JSON.stringify(payload),
  });
}
