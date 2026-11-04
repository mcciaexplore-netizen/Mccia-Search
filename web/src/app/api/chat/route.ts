import { NextRequest, NextResponse } from "next/server";
import { cleanHistory } from "@/lib/conversation";
import { clientAddress, proxyToBackend } from "@/lib/proxy";

export async function POST(req: NextRequest) {
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ detail: "Request body must be JSON." }, { status: 400 });
  }
  // The conversation memory (the last few question and answer pairs) is passed on, but only in the shape the backend expects and no
  // larger than it needs: whatever else the browser put in `history` is dropped.
  if (typeof body === "object" && body !== null && !Array.isArray(body)) {
    const { history, ...rest } = body as Record<string, unknown>;
    const turns = cleanHistory(history);
    body = turns.length ? { ...rest, history: turns } : rest;
  }
  return proxyToBackend("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...clientAddress(req) },
    body: JSON.stringify(body),
  });
}
