import { cookies } from "next/headers";
import { ssoSettings } from "./auth-config";
import { readSessionToken, SESSION_COOKIE } from "./session";
import type { HubUser } from "./sso";

/** The signed-in person on this request (server components and route handlers), or null. */
export async function getSessionUser(): Promise<HubUser | null> {
  const settings = ssoSettings();
  if (!settings.ok) return null;
  return readSessionToken(cookies().get(SESSION_COOKIE)?.value, settings.sessionSecret);
}
