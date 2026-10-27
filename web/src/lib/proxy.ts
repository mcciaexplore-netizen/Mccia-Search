import { NextResponse } from "next/server";

// The backend's address. Only the server reads it; set BACKEND_URL in web/.env.local.
const BACKEND = process.env.BACKEND_URL ?? "http://localhost:8002";

// The backend wants an API key on every route but /health. These two variables are read only
// here, on the server (no NEXT_PUBLIC_ prefix), so a key is never sent to a browser:
//   BACKEND_API_KEY    the chat and the page images
//   BACKEND_ADMIN_KEY  the document list, ingestion and the audit log
export type KeyKind = "user" | "admin";

function keyHeader(kind: KeyKind): Record<string, string> {
  const name = kind === "admin" ? "BACKEND_ADMIN_KEY" : "BACKEND_API_KEY";
  const key = process.env[name];
  if (!key) {
    console.error(`${name} is not set, so the backend will refuse this request.`);
    return {};
  }
  return { [kind === "admin" ? "X-Admin-Key" : "X-API-Key"]: key };
}

/** Forward a request to the backend and relay its JSON answer and status code. */
export async function proxyToBackend(
  path: string,
  init?: RequestInit,
  kind: KeyKind = "user"
): Promise<NextResponse> {
  try {
    const headers = { ...(init?.headers as Record<string, string> | undefined), ...keyHeader(kind) };
    const res = await fetch(`${BACKEND}${path}`, { ...init, headers, cache: "no-store" });
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
    const res = await fetch(`${BACKEND}${path}`, { headers: keyHeader("user"), cache: "no-store" });
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
