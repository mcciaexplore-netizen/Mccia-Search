import { NextResponse } from "next/server";

// The backend's address. Only the server reads it; set BACKEND_URL in web/.env.local.
const BACKEND = process.env.BACKEND_URL ?? "http://localhost:8002";

/** Forward a request to the backend and relay its JSON answer and status code. */
export async function proxyToBackend(
  path: string,
  init?: RequestInit
): Promise<NextResponse> {
  try {
    const res = await fetch(`${BACKEND}${path}`, { ...init, cache: "no-store" });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json(
      { detail: "The server could not be reached. Please try again." },
      { status: 502 }
    );
  }
}

/** The browser's address as the proxy sees it, so the backend's audit log can record who asked. */
export function clientAddress(req: Request & { ip?: string }): Record<string, string> {
  const forwarded = req.headers.get("x-forwarded-for") ?? req.ip ?? "";
  return forwarded ? { "X-Forwarded-For": forwarded } : {};
}
