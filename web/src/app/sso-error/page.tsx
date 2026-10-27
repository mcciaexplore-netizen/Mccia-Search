import Logo from "@/components/Logo";
import { ssoSettings } from "@/lib/auth-config";

export const dynamic = "force-dynamic";
export const metadata = { title: "Sign-in problem · MCCIA Search" };

const MESSAGES: Record<string, { title: string; text: string }> = {
  expired: { title: "That sign-in link has expired", text: "For your safety it works for only a minute. Open MCCIA Search from the App Hub again." },
  replayed: { title: "That sign-in link was already used", text: "Each link works once. Open MCCIA Search from the App Hub again to get a new one." },
  invalid: { title: "We could not sign you in", text: "The sign-in link was not valid. Open MCCIA Search from the App Hub again." },
  unavailable: { title: "MCCIA Search is not available right now", text: "The search server did not answer. Please try again in a minute." },
  config: { title: "Sign-in is not set up correctly", text: "Please tell your administrator." },
};

export default function SsoErrorPage({ searchParams }: { searchParams: { reason?: string } }) {
  const message = MESSAGES[searchParams.reason ?? ""] ?? MESSAGES.invalid;
  const settings = ssoSettings();
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center px-6 py-10 text-center">
      <Logo width={200} />
      <h1 className="mt-8 text-2xl font-semibold text-primary">{message.title}</h1>
      <p className="mt-3 max-w-md text-lg text-muted">{message.text}</p>
      {settings.ok && (
        <div className="mt-8 flex flex-wrap justify-center gap-3">
          <a href={settings.launchUrl} className="rounded-xl bg-accent px-5 py-2.5 font-semibold text-ink transition hover:brightness-110">Try again</a>
          <a href={settings.hubUrl} className="rounded-xl border border-primary/30 px-5 py-2.5 font-semibold text-primary transition hover:bg-primary/5">Back to App Hub</a>
        </div>
      )}
    </main>
  );
}
