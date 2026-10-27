// This app's own sign-in cookie: a short-lived JWT (HS256) signed with SESSION_SECRET.
// Pure (only `jose`), so it runs in the middleware (Edge) and in route handlers, and is tested directly.
import { jwtVerify, SignJWT } from "jose";
import { ROLES, type HubUser, type Role } from "./sso";

export const SESSION_COOKIE = "mccia_session";
/** A sign-in lasts two hours. The hub re-checks access each time a person is sent back to it, so this is also the longest a removed person can stay in. */
export const SESSION_SECONDS = 2 * 60 * 60;
const ISSUER = "mccia-search";
const MIN_SECRET_CHARS = 32;

const keyOf = (secret: string) => {
  if (secret.length < MIN_SECRET_CHARS) throw new Error(`SESSION_SECRET must be at least ${MIN_SECRET_CHARS} characters`);
  return new TextEncoder().encode(secret);
};

export async function createSessionToken(user: HubUser, secret: string, now: Date = new Date()): Promise<string> {
  const iat = Math.floor(now.getTime() / 1000);
  return new SignJWT({ email: user.email, name: user.name, role: user.role })
    .setProtectedHeader({ alg: "HS256" })
    .setSubject(user.id)
    .setIssuer(ISSUER)
    .setIssuedAt(iat)
    .setExpirationTime(iat + SESSION_SECONDS)
    .sign(keyOf(secret));
}

/** The signed-in person, or null for a missing, expired, tampered or foreign cookie. */
export async function readSessionToken(token: string | undefined, secret: string, now: Date = new Date()): Promise<HubUser | null> {
  if (!token) return null;
  try {
    const { payload } = await jwtVerify(token, keyOf(secret), { algorithms: ["HS256"], issuer: ISSUER, currentDate: now });
    const { sub, email, name, role } = payload;
    if (typeof sub !== "string" || typeof email !== "string" || typeof name !== "string" || !ROLES.includes(role as Role)) return null;
    return { id: sub, email, name, role: role as Role };
  } catch {
    return null;
  }
}
