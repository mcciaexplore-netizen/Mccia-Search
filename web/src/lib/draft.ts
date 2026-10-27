// The question being typed, kept in this browser so that nothing is lost when the page reloads, in particular when a
// session ends and the person is sent through the App Hub and back. Browser storage can be missing or blocked, so
// every use is guarded and the page works without it.
const DRAFT_KEY = "mccia.draft";
const RELAUNCH_KEY = "mccia.relaunchedAt";
const RELAUNCH_COOLDOWN_MS = 30_000; // never send someone round the loop more than once in half a minute

export function saveDraft(text: string): void {
  try {
    if (text) window.localStorage.setItem(DRAFT_KEY, text);
    else window.localStorage.removeItem(DRAFT_KEY);
  } catch {}
}

export function loadDraft(): string {
  try {
    return window.localStorage.getItem(DRAFT_KEY) ?? "";
  } catch {
    return "";
  }
}

/** True when this reload is the one that follows a session ending (so the page can say the question was kept). */
export function wasRelaunched(): boolean {
  try {
    const at = Number(window.sessionStorage.getItem(RELAUNCH_KEY) ?? 0);
    return at > 0 && Date.now() - at < 5 * 60_000;
  } catch {
    return false;
  }
}

/** Starts the trip through the hub, unless one was just made and did not help (then the person is told instead of looped). */
export function startRelaunch(): boolean {
  try {
    const last = Number(window.sessionStorage.getItem(RELAUNCH_KEY) ?? 0);
    if (last && Date.now() - last < RELAUNCH_COOLDOWN_MS) return false;
    window.sessionStorage.setItem(RELAUNCH_KEY, String(Date.now()));
  } catch {}
  return true;
}
