// Single sign-on settings, read from the environment on every call (so a change in Vercel needs only a redeploy).
// SSO_ENFORCE=true turns the sign-in requirement on. Anything else leaves the site working as it did before
// single sign-on, which is the way back if something goes wrong. When it is on and a setting is missing, the
// site refuses to open instead of quietly staying open.

/** Whether visitors must sign in through the App Hub. */
export const ssoEnforced = () => process.env.SSO_ENFORCE === "true";

export type SsoSettings =
  | { ok: true; hubUrl: string; audience: string; launchUrl: string; sessionSecret: string; publicKey: string }
  | { ok: false; problems: string[] };

export function ssoSettings(): SsoSettings {
  const problems: string[] = [];
  const hubUrl = (process.env.HUB_URL ?? "").trim().replace(/\/+$/, "");
  const audience = (process.env.SSO_AUDIENCE ?? "mccia-search").trim();
  const sessionSecret = process.env.SESSION_SECRET ?? "";
  const publicKey = (process.env.HUB_SSO_PUBLIC_KEY ?? "").trim();

  if (!/^https:\/\/[^/\s]+$/.test(hubUrl) && !/^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(hubUrl)) {
    problems.push("HUB_URL must be the hub's address, such as https://mccia-apphub.vercel.app");
  }
  if (!/^[a-z0-9]+(-[a-z0-9]+)*$/.test(audience)) problems.push("SSO_AUDIENCE must be the app's slug in the hub");
  if (sessionSecret.length < 32) problems.push("SESSION_SECRET must be at least 32 characters");
  if (!publicKey) problems.push("HUB_SSO_PUBLIC_KEY is not set");
  if (problems.length) return { ok: false, problems };
  return { ok: true, hubUrl, audience, launchUrl: `${hubUrl}/launch/${audience}`, sessionSecret, publicKey };
}
