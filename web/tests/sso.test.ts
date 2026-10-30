// Run: npm test   (node:test through tsx; no server, no network)
import assert from "node:assert/strict";
import { generateKeyPairSync, createSecretKey } from "node:crypto";
import { test } from "node:test";
import { importPKCS8, SignJWT } from "jose";
import { SsoError, verifyHubToken } from "../src/lib/sso";
import { createSessionToken, readSessionToken, SESSION_SECONDS } from "../src/lib/session";
import { ssoSettings } from "../src/lib/auth-config";

const pair = () => {
  const { privateKey, publicKey } = generateKeyPairSync("ed25519");
  return { priv: privateKey.export({ type: "pkcs8", format: "pem" }).toString(), pub: Buffer.from(publicKey.export({ type: "spki", format: "pem" })).toString("base64") };
};
const HUB = pair(), OTHER = pair();
const NOW = new Date("2026-10-27T12:00:00Z");
const nowS = Math.floor(NOW.getTime() / 1000);

async function token(over: Record<string, unknown> = {}, opts: { key?: string; kid?: string | null; alg?: string } = {}) {
  const claims = { email: "anita@mccia.test", name: "Anita Kulkarni", role: "user", ...over };
  const jwt = new SignJWT(claims as Record<string, unknown>)
    .setProtectedHeader({ alg: opts.alg ?? "EdDSA", typ: "JWT", ...(opts.kid === null ? {} : { kid: opts.kid ?? "hub-1" }) })
    .setIssuer("iss" in over ? (over.iss as string) : "mccia-app-hub")
    .setAudience("aud" in over ? (over.aud as string) : "mccia-search")
    .setSubject("sub" in over ? (over.sub as string) : "hub-user-1")
    .setIssuedAt("iat" in over ? (over.iat as number) : nowS)
    .setExpirationTime("exp" in over ? (over.exp as number) : nowS + 60);
  // a claim given as undefined is left out of the token, to test tokens that lack it
  if (!("jti" in over) || over.jti !== undefined) jwt.setJti("jti" in over ? (over.jti as string) : "jti-0000000001");
  return jwt.sign(await importPKCS8(opts.key ?? HUB.priv, "EdDSA"));
}
const verify = (t: string | null | undefined, over: { audience?: string; now?: Date; publicKey?: string } = {}) =>
  verifyHubToken(t, { publicKey: HUB.pub, audience: "mccia-search", now: NOW, ...over });
const rejects = async (p: Promise<unknown>, code: string) =>
  assert.rejects(p, (e: unknown) => e instanceof SsoError && e.code === code, `expected SsoError ${code}`);

test("a valid token signs the person in with the right name, email and role", async () => {
  const c = await verify(await token());
  assert.deepEqual({ id: c.id, email: c.email, name: c.name, role: c.role, jti: c.jti }, { id: "hub-user-1", email: "anita@mccia.test", name: "Anita Kulkarni", role: "user", jti: "jti-0000000001" });
  assert.equal((await verify(await token({ role: "admin" }))).role, "admin");
});
test("an expired token is rejected (as expired)", async () => rejects(verify(await token(), { now: new Date(NOW.getTime() + 66_000) }), "expired"));
test("a token a few seconds past expiry is still accepted (clock tolerance)", async () => assert.ok(await verify(await token(), { now: new Date(NOW.getTime() + 63_000) })));
test("a wrong audience is rejected", async () => rejects(verify(await token({ aud: "some-other-app" })), "invalid"));
test("a wrong issuer is rejected", async () => rejects(verify(await token({ iss: "someone-else" })), "invalid"));
test("a tampered token is rejected (payload edited, signature kept)", async () => {
  const [h, p, s] = (await token()).split(".");
  const edited = Buffer.from(JSON.stringify({ ...JSON.parse(Buffer.from(p, "base64url").toString()), role: "admin", sub: "someone-else" })).toString("base64url");
  await rejects(verify(`${h}.${edited}.${s}`), "invalid");
  await rejects(verify(`${h}.${p}.${s.slice(0, -4)}AAAA`), "invalid");
});
test("a token signed by a different key is rejected", async () => rejects(verify(await token({}, { key: OTHER.priv })), "invalid"));
test("algorithm confusion is rejected: HS256 using the public key as the secret", async () => {
  const forged = await new SignJWT({ email: "x@y.z", name: "X", role: "admin" }).setProtectedHeader({ alg: "HS256", kid: "hub-1" })
    .setIssuer("mccia-app-hub").setAudience("mccia-search").setSubject("evil").setJti("jti-forged-01").setIssuedAt(nowS).setExpirationTime(nowS + 60)
    .sign(createSecretKey(Buffer.from(HUB.pub, "base64")));
  await rejects(verify(forged), "invalid");
});
test("alg none is rejected", async () => {
  const body = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  await rejects(verify(`${body({ alg: "none" })}.${body({ iss: "mccia-app-hub", aud: "mccia-search", sub: "evil", email: "e@e.e", name: "E", role: "admin", jti: "jti-none-001", iat: nowS, exp: nowS + 60 })}.`), "invalid");
});
test("an unknown key id is rejected; no key id is accepted", async () => {
  await rejects(verify(await token({}, { kid: "hub-99" })), "invalid");
  assert.ok(await verify(await token({}, { kid: null })));
});
test("a long-lived token is rejected, even if the signature is good", async () => rejects(verify(await token({ exp: nowS + 3600 })), "invalid"));
test("a token from the future is rejected", async () => rejects(verify(await token({ iat: nowS + 600, exp: nowS + 660 })), "invalid"));
test("missing or odd claims are rejected", async () => {
  await rejects(verify(await token({ role: "root" })), "invalid");
  await rejects(verify(await token({ role: undefined })), "invalid");
  await rejects(verify(await token({ email: "" })), "invalid");
  await rejects(verify(await token({ jti: undefined })), "invalid");
  await rejects(verify(await token({ name: "x".repeat(300) })), "invalid");
});
test("no token, junk and oversize tokens are rejected", async () => {
  await rejects(verify(undefined), "missing"); await rejects(verify(""), "missing");
  await rejects(verify("not-a-token"), "invalid"); await rejects(verify("a.b.c"), "invalid");
  await rejects(verify("x".repeat(5000)), "invalid");
});
test("an unusable public key is a configuration error, not a sign-in error", async () => rejects(verify(await token(), { publicKey: "bm90IGEga2V5" }), "config"));
test("the error message never contains the token", async () => {
  const t = await token({ aud: "other" });
  try { await verify(t); assert.fail("should reject"); } catch (e) { assert.ok(!String((e as Error).message).includes(t.slice(10, 40))); }
});

const SECRET = "s".repeat(40);
const USER = { id: "hub-user-1", email: "anita@mccia.test", name: "Anita", role: "admin" as const };
test("the session cookie round-trips and lasts two hours", async () => {
  assert.equal(SESSION_SECONDS, 7200);
  const t = await createSessionToken(USER, SECRET, NOW);
  assert.deepEqual(await readSessionToken(t, SECRET, NOW), USER);
  assert.deepEqual(await readSessionToken(t, SECRET, new Date(NOW.getTime() + 7199_000)), USER);
  assert.equal(await readSessionToken(t, SECRET, new Date(NOW.getTime() + 7201_000)), null);
});
test("a tampered, foreign or wrong-secret session cookie is not a session", async () => {
  const t = await createSessionToken(USER, SECRET, NOW);
  assert.equal(await readSessionToken(t, "z".repeat(40), NOW), null);
  const [h, p, s] = t.split(".");
  const admin = Buffer.from(JSON.stringify({ ...JSON.parse(Buffer.from(p, "base64url").toString()), role: "admin", sub: "x" })).toString("base64url");
  assert.equal(await readSessionToken(`${h}.${admin}.${s}`, SECRET, NOW), null);
  assert.equal(await readSessionToken(await token(), SECRET, NOW), null); // a hub token is not a session
  assert.equal(await readSessionToken(undefined, SECRET), null);
  assert.equal(await readSessionToken("junk", SECRET), null);
});
test("a short session secret is refused", async () => assert.rejects(createSessionToken(USER, "short", NOW), /at least 32/));

test("SSO settings: off by default, complete when set, incomplete is reported (never silently open)", () => {
  const keep = { ...process.env };
  try {
    for (const k of ["HUB_URL", "SSO_AUDIENCE", "SESSION_SECRET", "HUB_SSO_PUBLIC_KEY"]) delete process.env[k];
    const bad = ssoSettings(); assert.equal(bad.ok, false);
    Object.assign(process.env, { HUB_URL: "https://mccia-apphub.vercel.app/", SESSION_SECRET: SECRET, HUB_SSO_PUBLIC_KEY: HUB.pub });
    const good = ssoSettings();
    assert.ok(good.ok && good.hubUrl === "https://mccia-apphub.vercel.app" && good.audience === "mccia-search" && good.launchUrl === "https://mccia-apphub.vercel.app/launch/mccia-search");
    process.env.HUB_URL = "http://evil.example.com"; assert.equal(ssoSettings().ok, false);
    process.env.HUB_URL = "http://localhost:3998"; assert.equal(ssoSettings().ok, true);
  } finally { process.env = keep; }
});

test("hub_url: kept from the token into the session, only as an http(s) address", async () => {
  assert.equal((await verify(await token({ hub_url: "https://mccia-apphub.vercel.app/" }))).hubUrl, "https://mccia-apphub.vercel.app");
  for (const bad of [undefined, "javascript:alert(1)", "not a url", 42]) assert.equal("hubUrl" in (await verify(await token({ hub_url: bad }))), false);
  const t = await createSessionToken({ ...USER, hubUrl: "https://mccia-apphub.vercel.app" }, SECRET, NOW);
  assert.equal((await readSessionToken(t, SECRET, NOW))?.hubUrl, "https://mccia-apphub.vercel.app");
  assert.equal("hubUrl" in ((await readSessionToken(await createSessionToken(USER, SECRET, NOW), SECRET, NOW)) ?? {}), false);
});
