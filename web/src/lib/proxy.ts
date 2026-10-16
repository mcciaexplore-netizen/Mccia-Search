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

/**
 * Forward a request to the backend and relay a file (a page image) as it is. The first
 * view of a page can take a while, so the body is passed on as a stream. Errors come
 * back as JSON with the backend's status code, like proxyToBackend.
 */
export async function proxyFileFromBackend(path: string): Promise<Response> {
  try {
    const res = await fetch(`${BACKEND}${path}`, { cache: "no-store" });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ detail: `Server error ${res.status}` }));
      return NextResponse.json(data, { status: res.status });
    }
    return new Response(res.body, {
      status: 200,
      headers: {
        "Content-Type": res.headers.get("content-type") ?? "application/octet-stream",
        "Cache-Control": res.headers.get("cache-control") ?? "no-store",
      },
    });
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
