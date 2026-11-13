"use client";

import type { Chat } from "@/lib/types";
import { dayGroup } from "@/lib/history";
import type { DayGroup } from "@/lib/history";

const GROUPS: DayGroup[] = ["Today", "Yesterday", "Earlier"];

function when(date: Date, group: DayGroup): string {
  return group === "Today"
    ? date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : date.toLocaleDateString([], { day: "numeric", month: "short" });
}

interface Props {
  chats: Chat[];
  activeId: string | null;
  onOpen: (id: string) => void;
  onDelete: (id: string) => void;
}

export default function HistoryList({ chats, activeId, onOpen, onDelete }: Props) {
  if (chats.length === 0) {
    return <p className="px-3 text-sm leading-relaxed text-muted">Your conversations will appear here.</p>;
  }

  const now = new Date();
  return (
    <nav aria-label="Chat history" className="space-y-4">
      {GROUPS.map((group) => {
        const inGroup = chats.filter((chat) => dayGroup(chat.updatedAt, now) === group);
        if (inGroup.length === 0) return null;
        return (
          <section key={group} aria-label={group}>
            <h2 className="px-3 pb-1 text-xs font-semibold uppercase tracking-wider text-muted">{group}</h2>
            <ul className="space-y-0.5">
              {inGroup.map((chat) => {
                const active = chat.id === activeId;
                return (
                  <li key={chat.id} className="group relative">
                    <button
                      type="button"
                      onClick={() => onOpen(chat.id)}
                      aria-current={active ? "true" : undefined}
                      title={chat.title}
                      className={`block w-full rounded-lg border-l-[3px] py-2 pl-3 pr-9 text-left transition-colors duration-150 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent ${
                        active ? "border-accent bg-tint" : "border-transparent hover:bg-tint"
                      }`}
                    >
                      <span className={`block truncate text-sm ${active ? "font-semibold text-ink" : "font-medium text-ink"}`}>{chat.title}</span>
                      <span className="block text-xs text-muted">{when(chat.updatedAt, group)}</span>
                    </button>
                    <button
                      type="button"
                      onClick={() => onDelete(chat.id)}
                      aria-label={`Delete chat: ${chat.title}`}
                      className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded-md p-1.5 text-muted opacity-0 transition hover:bg-line hover:text-ink focus-visible:opacity-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent group-focus-within:opacity-100 group-hover:opacity-100"
                    >
                      <svg aria-hidden="true" viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4">
                        <path d="M6.3 5.3a1 1 0 011.4 0L10 7.6l2.3-2.3a1 1 0 111.4 1.4L11.4 9l2.3 2.3a1 1 0 01-1.4 1.4L10 10.4l-2.3 2.3a1 1 0 01-1.4-1.4L8.6 9 6.3 6.7a1 1 0 010-1.4z" />
                      </svg>
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}
    </nav>
  );
}
