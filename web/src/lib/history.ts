import type { Chat, Message, Source } from "./types";

// Chat history lives in this browser's localStorage: nothing is sent to the server,
// and it is not shared between browsers or people.

export const STORAGE_KEY = "mccia.chats.v1";
export const MAX_CHATS = 50;
export const MAX_MESSAGES = 100;
const TITLE_CHARS = 48;

/** A chat's title: its first question, tidied and shortened. */
export function titleFor(messages: Message[]): string {
  const first = messages.find((m) => m.role === "user")?.content ?? "";
  const text = first.replace(/\s+/g, " ").trim();
  if (!text) return "New chat";
  return text.length > TITLE_CHARS ? text.slice(0, TITLE_CHARS - 1).trimEnd() + "…" : text;
}

function isDateString(value: unknown): value is string {
  return typeof value === "string" && !Number.isNaN(new Date(value).getTime());
}

function reviveMessage(raw: unknown): Message | null {
  if (typeof raw !== "object" || raw === null) return null;
  const m = raw as Record<string, unknown>;
  if (typeof m.id !== "string" || typeof m.content !== "string") return null;
  if (m.role !== "user" && m.role !== "assistant") return null;
  if (!isDateString(m.timestamp)) return null;
  const message: Message = {
    id: m.id,
    role: m.role,
    content: m.content,
    timestamp: new Date(m.timestamp),
  };
  if (Array.isArray(m.sources)) {
    message.sources = m.sources.filter(
      (s): s is Source =>
        typeof s === "object" && s !== null && typeof (s as { filename?: unknown }).filename === "string"
    );
  }
  if (typeof m.found === "boolean") message.found = m.found;
  return message;
}

function reviveChat(raw: unknown): Chat | null {
  if (typeof raw !== "object" || raw === null) return null;
  const c = raw as Record<string, unknown>;
  if (typeof c.id !== "string" || !c.id || !Array.isArray(c.messages) || !isDateString(c.updatedAt)) return null;
  const messages = c.messages.map(reviveMessage).filter((m): m is Message => m !== null);
  if (messages.length === 0) return null;
  const title = typeof c.title === "string" && c.title.trim() ? c.title : titleFor(messages);
  return { id: c.id, title, updatedAt: new Date(c.updatedAt), messages };
}

/** The saved chats, newest first. Anything unreadable is ignored, never thrown. */
export function loadChats(): Chat[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const data: unknown = JSON.parse(raw);
    if (!Array.isArray(data)) return [];
    const seen = new Set<string>();
    return data
      .map(reviveChat)
      .filter((c): c is Chat => c !== null && !seen.has(c.id) && !!seen.add(c.id))
      .sort((a, b) => b.updatedAt.getTime() - a.updatedAt.getTime())
      .slice(0, MAX_CHATS);
  } catch {
    return [];
  }
}

/** Save the chats, keeping the newest MAX_CHATS; if the browser's storage is full, drop the oldest until it fits. */
export function saveChats(chats: Chat[]): void {
  let keep = chats.slice(0, MAX_CHATS).map((c) => ({
    ...c,
    updatedAt: c.updatedAt.toISOString(),
    messages: c.messages.slice(-MAX_MESSAGES).map((m) => ({ ...m, timestamp: m.timestamp.toISOString() })),
  }));
  while (true) {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(keep));
      return;
    } catch {
      if (keep.length <= 1) return; // storage unavailable or blocked: history just is not kept
      keep = keep.slice(0, Math.ceil(keep.length / 2));
    }
  }
}

/** The list with this chat's messages saved under `id`, moved to the top. */
export function upsertChat(chats: Chat[], id: string, messages: Message[], now: Date = new Date()): Chat[] {
  const chat: Chat = { id, title: titleFor(messages), updatedAt: now, messages: messages.slice(-MAX_MESSAGES) };
  return [chat, ...chats.filter((c) => c.id !== id)].slice(0, MAX_CHATS);
}

export function removeChat(chats: Chat[], id: string): Chat[] {
  return chats.filter((c) => c.id !== id);
}

export type DayGroup = "Today" | "Yesterday" | "Earlier";

export function dayGroup(date: Date, now: Date = new Date()): DayGroup {
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((startOfDay(now) - startOfDay(date)) / 86_400_000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  return "Earlier";
}
