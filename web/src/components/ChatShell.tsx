"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import ChatWindow from "./ChatWindow";
import HistoryList from "./HistoryList";
import Logo from "./Logo";
import SettingsPanel from "./SettingsPanel";
import { loadChats, removeChat, saveChats, upsertChat } from "@/lib/history";
import type { Chat, Message } from "@/lib/types";

type View = "chat" | "settings";

// crypto.randomUUID() only exists in secure contexts, and this app may be served
// over plain HTTP on the office network.
const newChatId = () =>
  `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

export default function ChatShell() {
  const [chats, setChats] = useState<Chat[]>([]);
  const chatsRef = useRef<Chat[]>([]);
  const [activeId, setActiveId] = useState(newChatId);
  const [view, setView] = useState<View>("chat");
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // The history comes from this browser's storage, so it can only be read after mounting.
  useEffect(() => {
    const saved = loadChats();
    chatsRef.current = saved;
    setChats(saved);
  }, []);

  useEffect(() => {
    if (!sidebarOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSidebarOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [sidebarOpen]);

  const store = useCallback((next: Chat[]) => {
    chatsRef.current = next;
    setChats(next);
    saveChats(next);
  }, []);

  // A chat is saved from its first message on; this runs after every message.
  const handleMessages = useCallback(
    (id: string, messages: Message[]) => {
      if (messages.length > 0) store(upsertChat(chatsRef.current, id, messages));
    },
    [store]
  );

  const startNewChat = () => {
    // A new id remounts ChatWindow, which clears its messages and input and drops
    // any answer that is still on its way.
    setActiveId(newChatId());
    setView("chat");
    setSidebarOpen(false);
  };

  const openChat = (id: string) => {
    setActiveId(id);
    setView("chat");
    setSidebarOpen(false);
  };

  const deleteChat = (id: string) => {
    store(removeChat(chatsRef.current, id));
    if (id === activeId) startNewChat();
  };

  const clearHistory = () => {
    if (!window.confirm("Delete all saved conversations from this browser?")) return;
    store([]);
    startNewChat();
  };

  const openSettings = () => {
    setView("settings");
    setSidebarOpen(false);
  };

  const activeMessages = chats.find((chat) => chat.id === activeId)?.messages ?? [];

  return (
    <div className="min-h-dvh bg-page">
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/40 md:hidden"
          aria-hidden="true"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <aside
        id="sidebar"
        className={`fixed inset-y-0 left-0 z-40 flex w-72 flex-col bg-primary text-white transition-[transform,visibility] duration-200 md:visible md:translate-x-0 ${
          sidebarOpen ? "translate-x-0" : "invisible -translate-x-full"
        }`}
      >
        <div className="p-5 pb-3">
          <div className="rounded-xl bg-white px-4 py-3 shadow-sm">
            <Logo width={176} />
          </div>
          <p className="mt-2.5 text-base font-semibold tracking-wider text-accent">AI Search</p>

          <button
            type="button"
            onClick={startNewChat}
            className="mt-4 w-full rounded-lg bg-accent px-4 py-2.5 text-sm font-semibold text-ink transition hover:brightness-110 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
          >
            New Chat
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3 pt-1">
          <HistoryList
            chats={chats}
            activeId={view === "chat" ? activeId : null}
            onOpen={openChat}
            onDelete={deleteChat}
            onClearAll={clearHistory}
          />
        </div>

        <div className="border-t border-white/15 p-3">
          <button
            type="button"
            onClick={openSettings}
            aria-current={view === "settings" ? "page" : undefined}
            className={`flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left text-sm font-medium transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-white ${
              view === "settings" ? "bg-white/15" : "hover:bg-white/10"
            }`}
          >
            <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="h-5 w-5">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z" />
            </svg>
            Settings
          </button>
        </div>
      </aside>

      <main className="flex h-dvh flex-col md:pl-72">
        <header className="flex items-center gap-3 bg-primary px-4 py-3 text-white md:hidden">
          <button
            type="button"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open menu"
            aria-controls="sidebar"
            aria-expanded={sidebarOpen}
            className="-ml-1 rounded-md p-1.5 hover:bg-white/10 focus-visible:outline focus-visible:outline-2 focus-visible:outline-white"
          >
            <svg
              aria-hidden="true"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              className="h-6 w-6"
            >
              <path d="M4 6h16M4 12h16M4 18h16" />
            </svg>
          </button>
          <div className="rounded-md bg-white px-2.5 py-1.5">
            <Logo width={88} />
          </div>
          <span className="text-sm font-semibold text-accent">AI Search</span>
        </header>

        <div className="min-h-0 flex-1">
          {view === "settings" ? (
            <SettingsPanel />
          ) : (
            <ChatWindow
              key={activeId}
              initialMessages={activeMessages}
              onMessagesChange={(messages) => handleMessages(activeId, messages)}
            />
          )}
        </div>
      </main>
    </div>
  );
}
