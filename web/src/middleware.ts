import { NextResponse, type NextRequest } from "next/server";
import { ssoEnforced, ssoSettings } from "@/lib/auth-config";
import { readSessionToken, SESSION_COOKIE } from "@/lib/session";

// The gate in front of every page and /api route. With SSO_ENFORCE off it does nothing, so the site works as it did
// before single sign-on. With it on, no valid session means: a page goes to the App Hub, which sends the person
// straight back signed in; an /api call gets a 401 the page turns into the same trip (keeping what was being typed).
// If the settings are incomplete it refuses to open: it never quietly lets people in.
const OPEN = new Set(["/api/sso/callback", "/sso-error", "/api/logout"]);

export async function middleware(req: NextRequest) {
  if (!ssoEnforced()) return NextResponse.next();
  const { pathname } = req.nextUrl;
  if (OPEN.has(pathname)) return NextResponse.next();

  const settings = ssoSettings();
  if (!settings.ok) {
    console.error("[sso] not configured:", settings.problems.join("; "));
    return new NextResponse("Single sign-on is not set up. Please tell an administrator.", { status: 500, headers: { "Cache-Control": "no-store" } });
  }
  if (await readSessionToken(req.cookies.get(SESSION_COOKIE)?.value, settings.sessionSecret)) return NextResponse.next();

  if (pathname.startsWith("/api/")) {
    return NextResponse.json({ detail: "Your session has ended.", relaunch: true }, { status: 401, headers: { "Cache-Control": "no-store" } });
  }
  return NextResponse.redirect(settings.launchUrl, { status: 302, headers: { "Cache-Control": "no-store" } });
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.(?:png|jpg|jpeg|gif|svg|webp|ico|css|js|map|txt|woff2?)$).*)"],
};
