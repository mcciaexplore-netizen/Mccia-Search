import { NextRequest, NextResponse } from "next/server";
import { ssoSettings } from "@/lib/auth-config";
import { SESSION_COOKIE } from "@/lib/session";

export const dynamic = "force-dynamic";

/** Ends this app's session and returns to the App Hub. (The hub's own sign-in is the hub's to end.) */
export function POST(req: NextRequest) {
  const settings = ssoSettings();
  const response = NextResponse.redirect(settings.ok ? settings.hubUrl : new URL("/", req.url), { status: 303, headers: { "Cache-Control": "no-store" } });
  response.cookies.set(SESSION_COOKIE, "", { httpOnly: true, secure: req.nextUrl.protocol === "https:", sameSite: "lax", path: "/", maxAge: 0 });
  return response;
}
