import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { ssoEnforced, ssoSettings } from "./auth-config";
import { readSessionToken, SESSION_COOKIE } from "./session";

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

type Who = { headers: Record<string, string> } | { denied: NextResponse };

/**
 * Who is asking, for the backend (X-User-Id and X-User-Email, which it trusts only because this request also carries the
 * API key). With single sign-on off, everyone is "anonymous", as the site worked before. With it on, the session decides:
 * none is a 401 the page turns into a trip to the hub; an admin-only route for someone who is not an admin is a 403.
 */
async function who(kind: KeyKind): Promise<Who> {
  if (!ssoEnforced()) return { headers: { "X-User-Id": "anonymous" } };
  const settings = ssoSettings();
  if (!settings.ok) {
    console.error("[sso] not configured:", settings.problems.join("; "));
    return { denied: NextResponse.json({ detail: "Single sign-on is not set up. Please tell an administrator." }, { status: 500 }) };
  }
  const user = await readSessionToken(cookies().get(SESSION_COOKIE)?.value, settings.sessionSecret);
  if (!user) return { denied: NextResponse.json({ detail: "Your session has ended.", relaunch: true }, { status: 401 }) };
  if (kind === "admin" && user.role !== "admin") {
    return { denied: NextResponse.json({ detail: "You do not have access to this." }, { status: 403 }) };
  }
  // A header value must be plain text; an address with other characters is left out (the user's id still goes).
  return { headers: { "X-User-Id": user.id, ...(/^[\x20-\x7e]+$/.test(user.email) ? { "X-User-Email": user.email } : {}) } };
}

/** The backend refusing this app's own keys is a setup problem, not the person's: they must not be sent off to sign in again. */
const keysRefused = () => {
  console.error("[proxy] the backend refused this app's API key");
  return NextResponse.json({ detail: "The search server did not accept this application. Please tell an administrator." }, { status: 502 });
};

/** Forward a request to the backend and relay its JSON answer and status code. */
export async function proxyToBackend(
  path: string,
  init?: RequestInit,
  kind: KeyKind = "user"
): Promise<NextResponse> {
  const asker = await who(kind);
  if ("denied" in asker) return asker.denied;
  try {
    const headers = { ...(init?.headers as Record<string, string> | undefined), ...asker.headers, ...keyHeader(kind) };
    const res = await fetch(`${BACKEND}${path}`, { ...init, headers, cache: "no-store" });
    if (res.status === 401) return keysRefused();
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
  const asker = await who("user");
  if ("denied" in asker) return asker.denied;
  try {
    const res = await fetch(`${BACKEND}${path}`, { headers: { ...asker.headers, ...keyHeader("user") }, cache: "no-store" });
    if (res.status === 401) return keysRefused();
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
