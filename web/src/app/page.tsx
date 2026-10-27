import ChatShell from "@/components/ChatShell";
import { ssoEnforced, ssoSettings } from "@/lib/auth-config";
import { getSessionUser } from "@/lib/server-session";

export const dynamic = "force-dynamic"; // who is signed in is read from the cookie on every visit

export default async function Home() {
  const settings = ssoEnforced() ? ssoSettings() : null;
  const user = settings?.ok ? await getSessionUser() : null;
  return <ChatShell user={user ? { name: user.name, email: user.email, role: user.role } : null} hubUrl={settings?.ok ? settings.hubUrl : null} />;
}
