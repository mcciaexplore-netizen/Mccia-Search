// Verifying the sign-in token the App Hub issues. Pure (only `jose`), so it is tested directly.
// The token is a JWT signed with the hub's Ed25519 private key; this app holds only the public key,
// so it can check tokens but never make one.
import { decodeProtectedHeader, errors, importSPKI, jwtVerify } from "jose";

export const ISSUER = "mccia-app-hub";
export const ROLES = ["user", "admin"] as const;
export type Role = (typeof ROLES)[number];
export type HubUser = { id: string; email: string; name: string; role: Role };
export type HubClaims = HubUser & { jti: string; iat: number; exp: number };

const ACCEPTED_KEY_IDS = new Set(["hub-1"]); // add the next id here when the hub's key is rotated
const MAX_TOKEN_CHARS = 4096;
const MAX_LIFETIME_SECONDS = 120; // the hub issues 60; a long-lived token is not one of its own
const CLOCK_TOLERANCE_SECONDS = 5;

export type SsoErrorCode = "missing" | "invalid" | "expired" | "config";
export class SsoError extends Error {
  constructor(readonly code: SsoErrorCode, message: string) {
    super(message);
    this.name = "SsoError";
  }
}

const isText = (v: unknown, max: number): v is string => typeof v === "string" && v.length > 0 && v.length <= max;

/**
 * Checks signature (EdDSA only), issuer, audience, expiry and the claims this app relies on, and returns them.
 * `publicKey` is the hub's public key as base64 of its PEM (HUB_SSO_PUBLIC_KEY). Throws SsoError with a code the
 * callback turns into a clear page; the message never contains the token.
 */
export async function verifyHubToken(
  token: string | null | undefined,
  options: { publicKey: string; audience: string; now?: Date }
): Promise<HubClaims> {
  if (!token) throw new SsoError("missing", "No sign-in token was sent.");
  if (token.length > MAX_TOKEN_CHARS) throw new SsoError("invalid", "The sign-in token is too long.");

  let key;
  try {
    key = await importSPKI(atob(options.publicKey), "EdDSA");
  } catch {
    throw new SsoError("config", "The hub's public key is not usable.");
  }

  const now = options.now ?? new Date();
  let payload;
  try {
    const header = decodeProtectedHeader(token);
    if (header.kid !== undefined && !ACCEPTED_KEY_IDS.has(header.kid)) throw new SsoError("invalid", "Unknown signing key.");
    ({ payload } = await jwtVerify(token, key, {
      algorithms: ["EdDSA"],
      issuer: ISSUER,
      audience: options.audience,
      clockTolerance: CLOCK_TOLERANCE_SECONDS,
      currentDate: now,
    }));
  } catch (e) {
    if (e instanceof SsoError) throw e;
    if (e instanceof errors.JWTExpired) throw new SsoError("expired", "The sign-in token has expired.");
    throw new SsoError("invalid", "The sign-in token is not valid.");
  }

  const { sub, email, name, role, jti, iat, exp } = payload;
  if (!isText(sub, 100) || !isText(email, 254) || !isText(name, 200) || !isText(jti, 100)) {
    throw new SsoError("invalid", "The sign-in token is missing information.");
  }
  if (!ROLES.includes(role as Role)) throw new SsoError("invalid", "The sign-in token has an unknown role.");
  if (typeof iat !== "number" || typeof exp !== "number") throw new SsoError("invalid", "The sign-in token has no times.");
  if (exp - iat > MAX_LIFETIME_SECONDS) throw new SsoError("invalid", "The sign-in token lives too long.");
  if (iat > now.getTime() / 1000 + CLOCK_TOLERANCE_SECONDS * 6) throw new SsoError("invalid", "The sign-in token is from the future.");
  return { id: sub, email, name, role: role as Role, jti, iat, exp };
}
