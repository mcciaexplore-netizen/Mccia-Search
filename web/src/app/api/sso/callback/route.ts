import { NextRequest, NextResponse } from "next/server";
import { ssoSettings } from "@/lib/auth-config";
import { createSessionToken, SESSION_COOKIE, SESSION_SECONDS } from "@/lib/session";
import { SsoError, verifyHubToken } from "@/lib/sso";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BACKEND = process.env.BACKEND_URL ?? "http://localhost:8002";
const MAX_BODY_BYTES = 8192;

// A failure goes to a plain page that says what happened and links back to the hub. 303 turns the hub's POST into a GET.
const fail = (req: NextRequest, reason: string) =>
  NextResponse.redirect(new URL(`/sso-error?reason=${reason}`, req.url), { status: 303, headers: { "Cache-Control": "no-store" } });

/**
 * Where the App Hub sends a signed-in person (an auto-submitted form with one field, `token`). The token is checked
 * (signature, issuer, audience, expiry), then spent at the backend: its id can be used only once, so a copy of it is
 * refused. Only then does this app set its own session cookie.
 */
export async function POST(req: NextRequest) {
  const settings = ssoSettings();
  if (!settings.ok) {
    console.error("[sso] not configured:", settings.problems.join("; "));
    return fail(req, "config");
  }

  let token = "";
  try {
    if (Number(req.headers.get("content-length") ?? 0) > MAX_BODY_BYTES) return fail(req, "invalid");
    const field = (await req.formData()).get("token");
    token = typeof field === "string" ? field : "";
  } catch {
    return fail(req, "invalid");
  }

  let claims;
  try {
    claims = await verifyHubToken(token, { publicKey: settings.publicKey, audience: settings.audience });
  } catch (e) {
    const code = e instanceof SsoError ? e.code : "invalid";
    console.warn("[sso] sign-in refused:", code); // the code only, never the token
    return fail(req, code === "missing" ? "invalid" : code);
  }

  let res: Response;
  try {
    res = await fetch(`${BACKEND}/sso/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Admin-Key": process.env.BACKEND_ADMIN_KEY ?? "" },
      body: JSON.stringify({ jti: claims.jti, exp: claims.exp, hub_user_id: claims.id, email: claims.email, name: claims.name, role: claims.role }),
      cache: "no-store",
      signal: AbortSignal.timeout(15_000),
    });
  } catch {
    return fail(req, "unavailable");
  }
  if (res.status === 409) return fail(req, "replayed");
  if (!res.ok) {
    console.error("[sso] the backend refused the sign-in:", res.status);
    return fail(req, res.status >= 500 ? "unavailable" : "config");
  }

  const session = await createSessionToken({ id: claims.id, email: claims.email, name: claims.name, role: claims.role }, settings.sessionSecret);
  const response = NextResponse.redirect(new URL("/", req.url), { status: 303, headers: { "Cache-Control": "no-store" } });
  response.cookies.set(SESSION_COOKIE, session, {
    httpOnly: true,
    secure: req.nextUrl.protocol === "https:",
    sameSite: "lax",
    path: "/",
    maxAge: SESSION_SECONDS,
  });
  return response;
}

// Someone who opens the address by hand goes to the front door, which sorts out whether they need to sign in.
export function GET(req: NextRequest) {
  return NextResponse.redirect(new URL("/", req.url), { status: 303 });
}
